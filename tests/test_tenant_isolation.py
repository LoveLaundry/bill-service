"""Tenant isolation: Laundry A can never touch Laundry B's records.

Proves the platform contract end to end across gate passes, deliveries,
bills and payments: unknown ids and foreign ids both surface as 404 (no
probing oracle), listings exclude foreign rows, cross-tenant linkage is
refused before any mutation, and suspended snapshots fail closed.
"""
import pytest
from fastapi import HTTPException

from bill_service import tenancy
from bill_service.crypto_helper import encrypt_dict
from bill_service.models import BillCreate, BillItemIn, DeliveryCreate, DeliveryItem
from bill_service.routers import bills, gatepasses, deliveries
from bill_service.routers.bills import GATEPASS_SENSITIVE_FIELDS
from conftest import LAUNDRY_A, LAUNDRY_B, now, tenant_user


def _gp_item(name="Pillow", received=10):
    return {
        "item_name": name,
        "specification": None,
        "category": "Bed",
        "client_qty": received,
        "received_qty": received,
        "difference": 0,
        "rewashed": False,
        "unit_price": 1.0,
    }


async def _store_gp(mocked_db, number, client="Hilton Colombo", items=None, laundry_id=LAUNDRY_A):
    doc = {
        "laundry_id": laundry_id,
        "gate_pass_number": number,
        "client_name": client,
        "status": "RECEIVED",
        "receiving_date": now().isoformat(),
        "items": items or [_gp_item()],
        "notes": "",
        "created_at": now(),
        "updated_at": now(),
    }
    res = await mocked_db["gatepasses_collection"].insert_one(
        encrypt_dict(doc, GATEPASS_SENSITIVE_FIELDS)
    )
    return str(res.inserted_id)


def user_a(name="alice"):
    return tenant_user(name, LAUNDRY_A, "ADMIN")


def user_b(name="bob"):
    return tenant_user(name, LAUNDRY_B, "ADMIN")


# ── Unit: tenant resolution ───────────────────────────────────────────────


def test_resolve_requires_session_laundry():
    with pytest.raises(HTTPException) as exc:
        tenancy.resolve_laundry_id({"auth_id": "x", "role": "ADMIN"})
    assert exc.value.status_code == 400


def test_resolve_rejects_suspended_snapshot():
    caller = {
        "auth_id": "x",
        "role": "STAFF",
        "laundry_id": LAUNDRY_A,
        "laundries": [{"id": LAUNDRY_A, "role": "STAFF", "status": "SUSPENDED"}],
    }
    with pytest.raises(HTTPException) as exc:
        tenancy.resolve_laundry_id(caller)
    assert exc.value.status_code == 403


def test_resolve_rejects_unassigned_laundry():
    caller = {
        "auth_id": "x",
        "role": "STAFF",
        "laundry_id": LAUNDRY_B,
        "laundries": [{"id": LAUNDRY_A, "role": "STAFF", "status": "ACTIVE"}],
    }
    with pytest.raises(HTTPException) as exc:
        tenancy.resolve_laundry_id(caller)
    assert exc.value.status_code == 403


def test_platform_support_token_passes():
    caller = {
        "auth_id": "root",
        "role": "PLATFORM_ADMIN",
        "platform_admin": True,
        "support_entry": True,
        "laundry_id": LAUNDRY_B,
        "laundries": [],
    }
    assert tenancy.resolve_laundry_id(caller) == LAUNDRY_B


def test_scoped_query_shapes():
    assert tenancy.scoped_query({"a": 1}, LAUNDRY_B) == {"a": 1, "laundry_id": LAUNDRY_B}
    # LoveLaundry (sole historical tenant) also matches pre-tenancy documents.
    scoped = tenancy.scoped_query({"a": 1}, tenancy.LOVE_LAUNDRY_ID)
    assert scoped["$and"][0] == {"a": 1}
    assert {"laundry_id": tenancy.LOVE_LAUNDRY_ID} in scoped["$and"][1]["$or"]
    assert tenancy.scoped_query({}, tenancy.LOVE_LAUNDRY_ID)["$or"]


async def test_stamp_only_touches_unstamped(mocked_db):
    await mocked_db["bills_collection"].insert_many([
        {"n": 1},
        {"n": 2, "laundry_id": LAUNDRY_B},
        {"n": 3, "laundry_id": None},
    ])
    counts = await tenancy.stamp_missing_laundry_ids(
        {"bills": mocked_db["bills_collection"]}, LAUNDRY_A
    )
    assert counts == {"bills": 2}
    remaining = await mocked_db["bills_collection"].find_one({"n": 2})
    assert remaining["laundry_id"] == LAUNDRY_B
    stamped = await mocked_db["bills_collection"].find_one({"n": 1})
    assert stamped["laundry_id"] == LAUNDRY_A


# ── Cross-tenant denial ───────────────────────────────────────────────────


async def test_b_cannot_read_as_pass(mocked_db):
    gp_id = await _store_gp(mocked_db, "GP-X-1", laundry_id=LAUNDRY_A)
    with pytest.raises(HTTPException) as exc:
        await gatepasses.get_gate_pass(gp_id, user_b())
    assert exc.value.status_code == 404
    # Owner reads fine.
    seen = await gatepasses.get_gate_pass(gp_id, user_a())
    assert seen["gate_pass_number"] == "GP-X-1"


async def test_b_lists_exclude_as_passes(mocked_db):
    await _store_gp(mocked_db, "GP-X-2", laundry_id=LAUNDRY_A)
    await _store_gp(mocked_db, "GP-X-3", laundry_id=LAUNDRY_B)
    rows_a = await gatepasses.list_gate_passes(None, None, None, None, 0, 5000, user_a())
    rows_b = await gatepasses.list_gate_passes(None, None, None, None, 0, 5000, user_b())
    assert {g["gate_pass_number"] for g in rows_a} == {"GP-X-2"}
    assert {g["gate_pass_number"] for g in rows_b} == {"GP-X-3"}


async def test_b_cannot_bill_from_as_pass(mocked_db):
    gp_id = await _store_gp(mocked_db, "GP-X-4", laundry_id=LAUNDRY_A)
    from types import SimpleNamespace

    payload = BillCreate(
        client_name="Hilton Colombo",
        gate_pass_id=gp_id,
        items=[BillItemIn(item_name="Pillow", quantity=10, unit_price=1.0, category="Bed")],
        instant=True,
    )
    with pytest.raises(HTTPException) as exc:
        await bills.create_bill(payload, user_b(), request=SimpleNamespace(headers={}))
    assert exc.value.status_code == 404


async def test_b_cannot_deliver_from_as_pass(mocked_db):
    gp_id = await _store_gp(mocked_db, "GP-X-5", laundry_id=LAUNDRY_A)
    payload = DeliveryCreate(
        gate_pass_id=gp_id,
        client_name="Hilton Colombo",
        delivery_date=now(),
        delivered_by="Bob",
        received_by="Bob",
        items=[DeliveryItem(item_name="Pillow", quantity=5, gate_pass_id=gp_id)],
    )
    from types import SimpleNamespace

    with pytest.raises(HTTPException) as exc:
        await deliveries.create_delivery(payload, user_b(), request=SimpleNamespace(headers={}))
    assert exc.value.status_code in (400, 404)


async def test_b_cannot_pay_as_bill(mocked_db):
    from bill_service.models import PaymentCreate
    from types import SimpleNamespace

    gp_id = await _store_gp(mocked_db, "GP-X-6", laundry_id=LAUNDRY_A)
    payload = BillCreate(
        client_name="Hilton Colombo",
        gate_pass_id=gp_id,
        items=[BillItemIn(item_name="Pillow", quantity=10, unit_price=1.0, category="Bed")],
    )
    bill = await bills.create_bill(payload, user_a(), request=SimpleNamespace(headers={}))
    with pytest.raises(HTTPException) as exc:
        await bills.create_payment(
            bill["id"],
            PaymentCreate(amount=5.0, payment_method="CASH", payment_date=now()),
            user_b(),
            request=SimpleNamespace(headers={}),
        )
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await bills.list_payments_for_bill(bill["id"], user_b())
    assert exc.value.status_code == 404


async def test_unbilled_list_is_tenant_scoped(mocked_db):
    await _store_gp(mocked_db, "GP-X-7", laundry_id=LAUNDRY_A)
    await _store_gp(mocked_db, "GP-X-8", laundry_id=LAUNDRY_B)
    rows_b = await bills.get_unbilled_gatepasses(None, user_b())
    assert {u["gate_pass_number"] for u in rows_b} == {"GP-X-8"}
    rows_a = await bills.get_unbilled_gatepasses(None, user_a())
    assert {u["gate_pass_number"] for u in rows_a} == {"GP-X-7"}


def test_platform_admin_holds_full_capabilities():
    from bill_service.auth_helper import ROLE_CAPABILITIES, require_capability

    assert set(ROLE_CAPABILITIES["PLATFORM_ADMIN"]) == set(ROLE_CAPABILITIES["ADMIN"])
    dep = require_capability("payment:write")
    caller = {
        "auth_id": "root",
        "role": "PLATFORM_ADMIN",
        "platform_admin": True,
        "support_entry": True,
        "laundry_id": LAUNDRY_B,
        "laundries": [],
    }
    assert dep(caller) is caller

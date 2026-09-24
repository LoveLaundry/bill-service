"""Regression tests: rewashed items are free re-washes — never billable.

Covers the two behavioural guarantees surfaced in the recent bug report:
 * an explicit bill line for a rewashed item is rejected with a clear 400
   (both the gate-pass leg and the delivery leg of ``create_bill``);
 * ``GET /bills/unbilled-gatepasses`` reports rewashed quantities as
   received-but-not-billed instead of hiding them silently.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bill_service.crypto_helper import encrypt_dict
from bill_service.models import BillCreate, BillItemIn
from bill_service.routers import bills
from bill_service.routers.bills import GATEPASS_SENSITIVE_FIELDS, DELIVERY_SENSITIVE_FIELDS


def _now():
    return datetime.now(timezone.utc)


def _item(name: str, received: int, rewashed: bool = False) -> dict:
    return {
        "item_name": name,
        "specification": None,
        "category": "Bed",
        "client_qty": received,
        "received_qty": received,
        "difference": 0,
        "rewashed": rewashed,
        "unit_price": 1.0,
    }


async def _store_gp(mocked_db, items, client="Hilton Colombo", status="RECEIVED"):
    doc = {
        "gate_pass_number": f"GP-TEST-{datetime.now(timezone.utc).timestamp()}",
        "client_name": client,
        "status": status,
        "receiving_date": _now().isoformat(),
        "items": items,
        "notes": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    encrypted = encrypt_dict(doc, GATEPASS_SENSITIVE_FIELDS)
    res = await mocked_db["gatepasses_collection"].insert_one(encrypted)
    return str(res.inserted_id)


async def _store_delivery(mocked_db, gate_pass_id, items):
    doc = {
        "gate_pass_id": gate_pass_id,
        "client_name": "Hilton Colombo",
        "status": "DELIVERED",
        "notification_status": "SENT",
        "delivery_date": _now().isoformat(),
        "items": items,
        "notes": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    encrypted = encrypt_dict(doc, DELIVERY_SENSITIVE_FIELDS)
    res = await mocked_db["deliveries_collection"].insert_one(encrypted)
    return str(res.inserted_id)


def _auth_user(name="Alice"):
    return {"auth_id": f"u-{name}", "user_name": name, "role": "ADMIN"}


def _fake_request():
    return SimpleNamespace(headers={})


async def test_unbilled_list_reports_rewashed_as_received_but_excluded(mocked_db):
    gp_id = await _store_gp(
        mocked_db,
        [
            _item("Pillow", 10),
            _item("Duvet Cover", 7, rewashed=True),
        ],
    )

    result = await bills.get_unbilled_gatepasses(None, _auth_user())

    assert len(result) == 1
    gp = result[0]
    assert gp["id"] == gp_id

    # Only the non-rewashed item is billable…
    names = [i["item_name"] for i in gp["unbilled_items"]]
    assert names == ["Pillow"]
    assert gp["unbilled_items"][0]["unbilled_qty"] == 10

    # …and the rewashed item is reported as received, free, not billed.
    assert gp["total_rewashed_qty"] == 7
    rewashed = gp["rewashed_items"]
    assert len(rewashed) == 1
    assert rewashed[0]["item_name"] == "Duvet Cover"
    assert rewashed[0]["received_qty"] == 7


async def test_create_bill_rejects_explicit_rewashed_line_from_gate_pass(mocked_db):
    gp_id = await _store_gp(
        mocked_db,
        [
            _item("Pillow", 4),
            _item("Duvet Cover", 7, rewashed=True),
        ],
    )

    payload = BillCreate(
        client_name="Hilton Colombo",
        gate_pass_id=gp_id,
        items=[
            BillItemIn(item_name="Duvet Cover", quantity=7, unit_price=1.0, category="Bed"),
        ],
        instant=True,
    )

    with pytest.raises(HTTPException) as exc:
        await bills.create_bill(payload, _auth_user(), request=_fake_request())

    assert exc.value.status_code == 400
    assert "free re-wash" in exc.value.detail
    assert "Duvet Cover" in exc.value.detail


async def test_create_bill_rejects_rewashed_line_via_delivery_leg(mocked_db):
    gp_id = await _store_gp(
        mocked_db,
        [
            _item("Towel", 3),
            _item("Bath Sheet", 5, rewashed=True),
        ],
    )
    delivery_id = await _store_delivery(
        mocked_db,
        gp_id,
        [
            {"item_name": "Towel", "quantity": 3, "specification": None},
            {"item_name": "Bath Sheet", "quantity": 5, "specification": None},
        ],
    )

    payload = BillCreate(
        client_name="Hilton Colombo",
        delivery_ids=[delivery_id],
        items=[
            BillItemIn(item_name="Bath Sheet", quantity=5, unit_price=1.0, category="Bed"),
        ],
        instant=True,
    )

    with pytest.raises(HTTPException) as exc:
        await bills.create_bill(payload, _auth_user(), request=_fake_request())

    assert exc.value.status_code == 400
    assert "free re-wash" in exc.value.detail
    assert "Bath Sheet" in exc.value.detail


async def test_create_bill_allows_billable_share_of_mixed_name(mocked_db):
    # Same item name, but only the rewashed specification is free; the
    # normal specification must remain billable up to its received qty.
    gp_id = await _store_gp(
        mocked_db,
        [
            _item("Pillow", 6),
            dict(_item("Pillow", 4, rewashed=True), specification="XL"),
        ],
    )

    payload = BillCreate(
        client_name="Hilton Colombo",
        gate_pass_id=gp_id,
        items=[
            BillItemIn(item_name="Pillow", quantity=6, unit_price=1.0, category="Bed"),
        ],
        instant=True,
    )

    bill = await bills.create_bill(payload, _auth_user(), request=_fake_request())

    assert bill["payment_status"] == "PAID"
    assert len(bill["items"]) == 1
    assert bill["items"][0]["item_name"] == "Pillow"
    assert bill["items"][0]["quantity"] == 6
    assert bill["grand_total"] == 6.0
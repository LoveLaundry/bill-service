"""Tests for the controlled gate-pass adjustment workflow.

Covers request staging, the second-user approval gate, and the critical
status re-derivation fix: approving a correction MUST account for recorded
deliveries/returns (previously they were ignored, so a pass whose outstanding
was fully delivered could never resolve to DELIVERED).
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import decrypt_dict, encrypt_dict
from bill_service.models import GatePassAdjustment, GatePassAdjustmentRequest
from bill_service.routers import adjustments, gatepasses

from conftest import auth_user

GP_SENSITIVE = ["client_name", "items", "notes"]
DL_SENSITIVE = ["client_name", "items", "notes"]
BILL_SENSITIVE = ["client_name", "notes", "items"]


def _now():
    return datetime.now(timezone.utc)


def _gp_item(*, name="Pillow", spec=None, client=30, received=30) -> dict:
    return {
        "item_name": name,
        "category": "Bed",
        "specification": spec,
        "client_qty": client,
        "received_qty": received,
        "difference": received - client,
        "mismatch_reason": None,
        "mismatch_notes": None,
    }


async def seed_gp(mocked_db, *, items, status="RECEIVED", gp_num=None) -> str:
    doc = {
        "gate_pass_number": gp_num or f"GP-{ObjectId()}",
        "client_name": "Test Client",
        "receiving_date": _now(),
        "received_by": "System",
        "items": items,
        "status": status,
        "notes": "",
        "quotation_id": None,
        "created_at": _now(),
        "updated_at": _now(),
        "adjustments": [],
    }
    res = await mocked_db["gatepasses_collection"].insert_one(encrypt_dict(doc, GP_SENSITIVE))
    return str(res.inserted_id)


async def seed_delivery(mocked_db, *, gp_id: str, item_spec, qty: int, status: str = "DELIVERED") -> str:
    doc = {
        "gate_pass_id": gp_id,
        "client_name": "Test Client",
        "delivery_date": _now(),
        "delivered_by": "Rider",
        "received_by": "Client",
        "items": [{"item_name": "Pillow", "specification": item_spec, "quantity": qty}],
        "status": status,
        "notes": "",
        "created_at": _now(),
    }
    res = await mocked_db["deliveries_collection"].insert_one(encrypt_dict(doc, DL_SENSITIVE))
    return str(res.inserted_id)


async def seed_bill(mocked_db, *, gp_id: str, qty: int, unit_price: int = 100) -> str:
    line_total = round(unit_price * qty, 2)
    doc = {
        "gate_pass_id": gp_id,
        "client_name": "Test Client",
        "items": [{"item_name": "Pillow", "category": "Bed", "unit_price": unit_price, "quantity": qty, "line_total": line_total}],
        "total_amount": line_total,
        "total_quantity": float(qty),
        "grand_total": line_total,
        "outstanding_amount": line_total,
        "discounts": 0,
        "transport_fee": 0,
        "taxes": 0,
        "additional_charges": 0,
        "paid_amount": 0,
        "notes": "",
        "payment_status": "PENDING",
        "created_at": _now(),
        "updated_at": _now(),
    }
    res = await mocked_db["bills_collection"].insert_one(encrypt_dict(doc, BILL_SENSITIVE))
    return str(res.inserted_id)


async def read_gp(mocked_db, gp_id: str) -> dict:
    raw = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    return decrypt_dict(raw, GP_SENSITIVE)


async def read_bill(mocked_db, bill_id: str) -> dict:
    raw = await mocked_db["bills_collection"].find_one({"_id": ObjectId(bill_id)})
    return decrypt_dict(raw, BILL_SENSITIVE)


async def test_request_creation_does_not_mutate_gate_pass(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(spec="Large", received=30)])

    req = GatePassAdjustmentRequest(
        gate_pass_id=gp_id, item_name="Pillow", specification="Large",
        corrected_qty=22, reason="damaged goods",
    )
    created = await adjustments.create_adjustment_request(req, current_user=auth_user("Alice"))

    assert created["status"] == "REQUESTED"
    assert created["original_qty"] == 30
    assert created["item_name"] == "Pillow"
    assert created["requested_by"] == "Alice"

    gp = await read_gp(mocked_db, gp_id)
    assert gp["items"][0]["received_qty"] == 30  # untouched


async def test_request_targets_spec_variant(mocked_db):
    gp_id = await seed_gp(
        mocked_db,
        items=[_gp_item(spec="Large", received=30), _gp_item(spec="Small", received=10)],
    )

    req = GatePassAdjustmentRequest(
        gate_pass_id=gp_id, item_name="Pillow", specification="Small", corrected_qty=4, reason="fix count",
    )
    created = await adjustments.create_adjustment_request(req, current_user=auth_user("Alice"))
    assert created["original_qty"] == 10


async def test_request_for_missing_item_is_404(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(spec="Large", received=30)])
    req = GatePassAdjustmentRequest(
        gate_pass_id=gp_id, item_name="Towel", corrected_qty=5, reason="nope",
    )
    with pytest.raises(HTTPException) as exc:
        await adjustments.create_adjustment_request(req, current_user=auth_user("Alice"))
    assert exc.value.status_code == 404


async def test_request_on_cancelled_gate_pass_is_409(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(spec="Large", received=30)], status="CANCELLED")
    req = GatePassAdjustmentRequest(
        gate_pass_id=gp_id, item_name="Pillow", corrected_qty=22, reason="too late",
    )
    with pytest.raises(HTTPException) as exc:
        await adjustments.create_adjustment_request(req, current_user=auth_user("Alice"))
    assert exc.value.status_code == 409


async def test_approve_requires_different_user(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(spec="Large", received=30)])
    req = GatePassAdjustmentRequest(
        gate_pass_id=gp_id, item_name="Pillow", specification="Large", corrected_qty=22, reason="damaged",
    )
    created = await adjustments.create_adjustment_request(req, current_user=auth_user("Alice"))

    with pytest.raises(HTTPException) as exc:
        await adjustments.approve_adjustment(created["id"], current_user=auth_user("Alice"))
    assert exc.value.status_code == 400


async def test_approve_applies_correction_and_status_accounts_for_deliveries(mocked_db):
    """Received 50, delivered 30, PARTIALLY_DELIVERED. Correct received to 30.

    Real delivered quantities (30) resolve the pass to DELIVERED. The old
    code recomputed with {} deliveries and could never reach DELIVERED.
    """
    gp_id = await seed_gp(
        mocked_db,
        items=[_gp_item(spec="Large", client=50, received=50)],
        status="PARTIALLY_DELIVERED",
    )
    await seed_delivery(mocked_db, gp_id=gp_id, item_spec="Large", qty=30)
    bill_id = await seed_bill(mocked_db, gp_id=gp_id, qty=50)

    req = GatePassAdjustmentRequest(
        gate_pass_id=gp_id, item_name="Pillow", specification="Large",
        corrected_qty=30, reason="recount showed 30",
    )
    created = await adjustments.create_adjustment_request(req, current_user=auth_user("Alice"))
    resp = await adjustments.approve_adjustment(created["id"], current_user=auth_user("Bob"))

    assert resp["status"] == "APPROVED"

    gp = await read_gp(mocked_db, gp_id)
    assert gp["items"][0]["received_qty"] == 30
    assert gp["items"][0]["difference"] == -20  # 30 corrected vs 50 client_qty
    assert gp["status"] == "DELIVERED"  # the fix
    assert gp["adjustments"][0]["approved_by"] == "Bob"
    assert gp["adjustments"][0]["original_value"] == 50
    assert gp["adjustments"][0]["corrected_value"] == 30

    bill = await read_bill(mocked_db, bill_id)
    assert bill["items"][0]["quantity"] == 30
    assert bill["grand_total"] == 3000

    adj_row = await mocked_db["adjustments_collection"].find_one({"gate_pass_id": gp_id})
    assert adj_row["status"] == "APPROVED"
    assert adj_row["approved_by_id"] == "u-Bob"


async def test_legacy_adjust_endpoint_stages_request_and_returns_unchanged_gate_pass(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(spec="Large", received=30)])

    resp = await gatepasses.adjust_gate_pass(
        gp_id,
        GatePassAdjustment(item_name="Pillow", specification="Large", corrected_qty=17, reason="count fix"),
        current_user=auth_user("Alice"),
    )

    assert resp["id"] == gp_id
    gp = await read_gp(mocked_db, gp_id)
    assert gp["items"][0]["received_qty"] == 30  # still unchanged

    requests = await mocked_db["adjustments_collection"].find({"gate_pass_id": gp_id}).to_list(None)
    assert len(requests) == 1
    assert requests[0]["status"] == "REQUESTED"
    assert requests[0]["specification"] == "Large"
    assert requests[0]["corrected_qty"] == 17


async def test_legacy_adjust_on_delivered_pass_creates_request_not_inline_edit(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(spec="Large", received=30)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, item_spec="Large", qty=30)

    resp = await gatepasses.adjust_gate_pass(
        gp_id,
        GatePassAdjustment(item_name="Pillow", specification="Large", corrected_qty=22, reason="post-delivery fix"),
        current_user=auth_user("Alice"),
    )

    gp = await read_gp(mocked_db, gp_id)
    assert gp["items"][0]["received_qty"] == 30  # never rewritten inline
    assert gp["status"] == "DELIVERED"
    count = await mocked_db["adjustments_collection"].count_documents({"gate_pass_id": gp_id})
    assert count == 1


async def test_reject_does_not_change_quantities(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(spec="Large", received=30)])
    req = GatePassAdjustmentRequest(
        gate_pass_id=gp_id, item_name="Pillow", specification="Large", corrected_qty=5, reason="wrong",
    )
    created = await adjustments.create_adjustment_request(req, current_user=auth_user("Alice"))

    resp = await adjustments.reject_adjustment(created["id"], current_user=auth_user("Bob"))
    assert resp["status"] == "REJECTED"

    gp = await read_gp(mocked_db, gp_id)
    assert gp["items"][0]["received_qty"] == 30
    adj_row = await mocked_db["adjustments_collection"].find_one({"gate_pass_id": gp_id})
    assert adj_row["status"] == "REJECTED"


async def test_approve_twice_is_409(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(spec="Large", received=30)])
    req = GatePassAdjustmentRequest(
        gate_pass_id=gp_id, item_name="Pillow", specification="Large", corrected_qty=29, reason="recount",
    )
    created = await adjustments.create_adjustment_request(req, current_user=auth_user("Alice"))
    await adjustments.approve_adjustment(created["id"], current_user=auth_user("Bob"))

    with pytest.raises(HTTPException) as exc:
        await adjustments.approve_adjustment(created["id"], current_user=auth_user("Carol"))
    assert exc.value.status_code == 409
"""Tests for the second-pass fixes: the approval handshake, the model guards,
duplicate gate-pass rows, and multi-gate-pass bill clamping."""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import decrypt_dict, encrypt_dict
from bill_service.models import GatePassAdjustmentRequest
from bill_service.routers import adjustments as adj
from bill_service.routers import gatepasses as gp_mod

from conftest import auth_user

# The approver must differ from the requester seeded as "u-Alice".
def auth_user_bob() -> dict:
    return auth_user("Bob")

GP_SENSITIVE = ["client_name", "items", "notes"]


def _now():
    return datetime.now(timezone.utc)


async def seed_gp(mocked_db, *, items, status="RECEIVED") -> str:
    doc = {
        "gate_pass_number": f"GP-{ObjectId()}",
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


def _item(name="Pillow", spec=None, client=30, received=30) -> dict:
    return {
        "item_name": name,
        "category": "Bed",
        "specification": spec,
        "client_qty": client,
        "received_qty": received,
        "difference": received - client,
    }


async def seed_request(mocked_db, *, gp_id, corrected=25, requested_by="u-Alice") -> str:
    res = await mocked_db["adjustments_collection"].insert_one(
        {
            "gate_pass_id": gp_id,
            "item_name": "Pillow",
            "specification": None,
            "corrected_qty": corrected,
            "reason": "MISCOUNT",
            "status": "REQUESTED",
            "requested_by": "Alice",
            "requested_by_id": requested_by,
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    return str(res.inserted_id)


# --- Approval is claimed by exactly one person ---
async def test_second_approver_is_refused(mocked_db):
    """Two supervisors pressing Approve must not apply the correction twice."""
    gp_id = await seed_gp(mocked_db, items=[_item(received=30)])
    adj_id = await seed_request(mocked_db, gp_id=gp_id, corrected=25)

    await adj.approve_adjustment(adj_id, auth_user_bob())
    # Second attempt: the request is no longer REQUESTED.
    with pytest.raises(HTTPException) as exc:
        await adj.approve_adjustment(adj_id, auth_user())
    assert exc.value.status_code == 409

    # And the correction landed exactly once.
    doc = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    items = decrypt_dict(doc, GP_SENSITIVE)["items"]
    assert items[0]["received_qty"] == 25
    assert len(decrypt_dict(doc, GP_SENSITIVE)["adjustments"]) == 1


async def test_approval_records_the_claim_and_the_outcome(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(received=30)])
    adj_id = await seed_request(mocked_db, gp_id=gp_id)

    out = await adj.approve_adjustment(adj_id, auth_user_bob())
    assert out["status"] == "APPROVED"

    row = await mocked_db["adjustments_collection"].find_one({"_id": ObjectId(adj_id)})
    assert row["status"] == "APPROVED"
    assert row["bill_sync"] == "OK"
    assert "approving_started_at" in row


async def test_requester_cannot_approve_their_own_request(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(received=30)])
    adj_id = await seed_request(mocked_db, gp_id=gp_id, requested_by="u-Alice")

    with pytest.raises(HTTPException) as exc:
        await adj.approve_adjustment(adj_id, auth_user())
    assert exc.value.status_code == 400
    assert "different user" in exc.value.detail

    row = await mocked_db["adjustments_collection"].find_one({"_id": ObjectId(adj_id)})
    assert row["status"] == "REQUESTED"


async def test_reject_cannot_overwrite_an_in_flight_approval(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(received=30)])
    adj_id = await seed_request(mocked_db, gp_id=gp_id)
    # Simulate approval having claimed the request.
    await mocked_db["adjustments_collection"].update_one(
        {"_id": ObjectId(adj_id)}, {"$set": {"status": "APPROVING"}}
    )

    with pytest.raises(HTTPException) as exc:
        await adj.reject_adjustment(adj_id, auth_user_bob())
    assert exc.value.status_code == 409
    row = await mocked_db["adjustments_collection"].find_one({"_id": ObjectId(adj_id)})
    assert row["status"] == "APPROVING"


# --- Bill sync failure is no longer hidden behind a cheerful APPROVED ---
async def test_bill_sync_failure_is_reported_not_swallowed(mocked_db, monkeypatch):
    gp_id = await seed_gp(mocked_db, items=[_item(received=30)])
    adj_id = await seed_request(mocked_db, gp_id=gp_id)

    async def boom(*args, **kwargs):
        raise RuntimeError("mongo write concern timeout")

    monkeypatch.setattr(adj, "sync_bills_to_gate_pass", boom)

    out = await adj.approve_adjustment(adj_id, auth_user_bob())
    # The correction IS recorded, and the caller is told the bill disagrees.
    assert out["status"] == "APPROVED"
    assert "bill_sync" in out and "FAILED" in out["bill_sync"]

    row = await mocked_db["adjustments_collection"].find_one({"_id": ObjectId(adj_id)})
    assert row["bill_sync"] == "FAILED"


# --- Model guards ---
def test_a_correction_must_actually_change_something():
    with pytest.raises(ValueError):
        GatePassAdjustmentRequest(
            gate_pass_id="x", item_name="Pillow", corrected_qty=0, reason="MISCOUNT"
        )
    assert (
        GatePassAdjustmentRequest(
            gate_pass_id="x", item_name="Pillow", corrected_qty=5, reason="MISCOUNT"
        ).corrected_qty
        == 5
    )


def test_a_correction_still_needs_a_reason():
    with pytest.raises(ValueError):
        GatePassAdjustmentRequest(gate_pass_id="x", item_name="Pillow", corrected_qty=5, reason="")


# --- Duplicate gate-pass rows are refused at the boundary ---
def _create_payload(items):
    from bill_service.models import GatePassCreate, GatePassItem

    return GatePassCreate(
        gate_pass_number=f"GP-{ObjectId()}",
        client_name="Test Client",
        receiving_date=_now(),
        received_by="System",
        items=[GatePassItem(**i) for i in items],
    )


class _StubRequest:
    headers: dict = {}


async def test_duplicate_item_rows_are_refused_on_create(mocked_db):
    payload = _create_payload(
        [
            {"item_name": "Pillow", "category": "Bed", "client_qty": 10, "received_qty": 10},
            {"item_name": "Pillow", "category": "Bed", "client_qty": 5, "received_qty": 5},
        ]
    )
    with pytest.raises(HTTPException) as exc:
        await gp_mod.create_gate_pass(payload, auth_user(), _StubRequest())
    assert exc.value.status_code == 400
    assert "more than once" in exc.value.detail


async def test_same_name_different_spec_is_fine(mocked_db):
    payload = _create_payload(
        [
            {"item_name": "Towel", "category": "Bath", "specification": "Large",
             "client_qty": 10, "received_qty": 10},
            {"item_name": "Towel", "category": "Bath", "specification": "Small",
             "client_qty": 5, "received_qty": 5},
        ]
    )
    out = await gp_mod.create_gate_pass(payload, auth_user(), _StubRequest())
    assert len(out["items"]) == 2


async def test_duplicate_item_rows_are_refused_on_update(mocked_db):
    from bill_service.models import GatePassItem, GatePassUpdate

    gp_id = await seed_gp(mocked_db, items=[_item(received=30)])
    payload = GatePassUpdate(
        items=[
            GatePassItem(item_name="Pillow", category="Bed", client_qty=10, received_qty=10),
            GatePassItem(item_name="Pillow", category="Bed", client_qty=5, received_qty=5),
        ]
    )
    with pytest.raises(HTTPException) as exc:
        await gp_mod.update_gate_pass(gp_id, payload, auth_user())
    assert exc.value.status_code == 400
    assert "more than once" in exc.value.detail


# --- Legacy duplicate rows still read correctly ---
async def test_legacy_duplicate_rows_still_add_up(mocked_db):
    """Older records already contain duplicates. They must still total up
    rather than one row silently winning."""
    from bill_service.services import balance_engine as be

    gp_id = await seed_gp(
        mocked_db, items=[_item(name="Pillow", received=10), _item(name="Pillow", received=6)]
    )
    balance = await gp_mod.get_gate_pass_balance(gp_id, auth_user())
    row = balance["items"][0]
    assert row["received_qty"] == 16
    assert row["outstanding_delivery_qty"] == 16
    assert balance["totals"]["received_qty"] == 16

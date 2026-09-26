"""Tests for delivery creation: the balance limit, and the status it derives.

A delivery is the only thing that moves pieces off a gate pass, so its limit has
to be the same number every other screen shows. These cover the four ways the
validator used to disagree with the engine.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import encrypt_dict
from bill_service.routers import deliveries as dl
from bill_service.routers import gatepasses as gp_mod

from conftest import auth_user

GP_SENSITIVE = ["client_name", "items", "notes"]


class _StubRequest:
    """Minimal stand-in for the injected Request; no idempotency key set."""

    headers: dict = {}


def _req() -> _StubRequest:
    return _StubRequest()


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
    }


async def seed_gp(mocked_db, *, items, status="RECEIVED", gp_id=None) -> str:
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
    return gp_id or str(res.inserted_id)


async def seed_delivery(mocked_db, *, gp_id: str, qty: int, name="Pillow", spec=None) -> str:
    doc = {
        "gate_pass_id": gp_id,
        "client_name": "Test Client",
        "delivery_date": _now(),
        "delivered_by": "Rider",
        "received_by": "Client",
        "items": [{"item_name": name, "specification": spec, "quantity": qty}],
        "status": "DELIVERED",
        "notes": "",
        "created_at": _now(),
    }
    res = await mocked_db["deliveries_collection"].insert_one(encrypt_dict(doc, GP_SENSITIVE))
    return str(res.inserted_id)


async def seed_return(
    mocked_db, *, gp_id: str, qty: int, name="Pillow", spec=None, resend_status="PENDING"
) -> None:
    """A RECEIVE_BACK return. `resend_status` SENT means already re-delivered."""
    doc = {
        "gate_pass_id": gp_id,
        "client_name": "Test Client",
        "return_date": _now(),
        "returned_by": "Client",
        "items": [
            {
                "item_name": name,
                "specification": spec,
                "returned_qty": qty,
                "action": "RECEIVE_BACK",
                "resend_status": resend_status,
            }
        ],
        "notes": "",
        "created_at": _now(),
    }
    await mocked_db["returns_collection"].insert_one(encrypt_dict(doc, GP_SENSITIVE))


async def seed_adjustment(mocked_db, *, gp_id: str, qty: int, name="Pillow", spec=None) -> None:
    await mocked_db["balance_adjustments_collection"].insert_one(
        {
            "gate_pass_id": gp_id,
            "delivery_id": None,
            "item_name": name,
            "specification": spec or "",
            "quantity": qty,
            "reason": "PIECES_MISSING_IN_TRANSIT",
            "notes": None,
            "status": "POSTED",
            "created_by": "Alice",
            "created_at": _now(),
        }
    )


def _payload(gp_id: str, items, **overrides):
    base = {
        "gate_pass_id": gp_id,
        "client_name": "Test Client",
        "delivery_date": _now(),
        "delivered_by": "Rider",
        "received_by": "Client",
        "items": items,
    }
    base.update(overrides)
    from bill_service.models import DeliveryCreate

    return DeliveryCreate(**base)


def _item(qty: int, name="Pillow", spec=None):
    return {"item_name": name, "specification": spec, "quantity": qty}


async def _create(gp_id, items, user=None):
    return await dl.create_delivery(_payload(gp_id, items), user or auth_user(), _req())


# --- The limit is the engine's outstanding, not received-minus-delivered ---
async def test_returned_pieces_are_deliverable_again(mocked_db):
    """Pieces the client sent back are back in our hands and can go out again."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20)
    # 30 received - 20 delivered + 10 returned = 20 still with us.
    await seed_return(mocked_db, gp_id=gp_id, qty=10)

    out = await _create(gp_id, [_item(15)])
    assert out["items"][0]["quantity"] == 15


async def test_a_credit_correction_raises_what_can_be_delivered(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=30)
    # 30 - 30 = 0 outstanding; a +5 credit for lost pieces makes 5 deliverable.
    await seed_adjustment(mocked_db, gp_id=gp_id, qty=5)

    out = await _create(gp_id, [_item(5)])
    assert out["items"][0]["quantity"] == 5


async def test_a_debit_correction_blocks_over_delivery(mocked_db):
    """A debit says these pieces were never owed; sending them is not allowed."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=25)
    # 30 - 25 = 5 outstanding, then a -5 debit squares the pass off.
    await seed_adjustment(mocked_db, gp_id=gp_id, qty=-5)

    with pytest.raises(HTTPException) as exc:
        await _create(gp_id, [_item(1)])
    assert exc.value.status_code == 400
    assert "Only 0 available" in exc.value.detail


async def test_duplicate_lines_in_one_payload_cannot_exceed_the_balance(mocked_db):
    """Each line was checked against the same figure, so two lines doubled up."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=10)])

    with pytest.raises(HTTPException) as exc:
        await _create(gp_id, [_item(6), _item(6)])
    assert exc.value.status_code == 400
    assert "Only 4 available" in exc.value.detail


async def test_duplicate_lines_within_the_balance_are_accepted(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=10)])
    out = await _create(gp_id, [_item(4), _item(6)])
    assert sum(i["quantity"] for i in out["items"]) == 10


async def test_specification_is_part_of_the_item_identity(mocked_db):
    gp_id = await seed_gp(
        mocked_db, items=[_gp_item(name="Towel", spec="Large", received=10)]
    )
    with pytest.raises(HTTPException) as exc:
        await _create(gp_id, [_item(1, name="Towel", spec="Small")])
    assert exc.value.status_code == 400
    assert "was not received" in exc.value.detail


# --- A cancelled pass has no live balance ---
async def test_cannot_deliver_against_a_cancelled_gate_pass(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item()], status="CANCELLED")
    with pytest.raises(HTTPException) as exc:
        await _create(gp_id, [_item(5)])
    assert exc.value.status_code == 409
    assert "cancelled" in exc.value.detail.lower()


async def test_unknown_gate_pass_is_404(mocked_db):
    with pytest.raises(HTTPException) as exc:
        await _create(str(ObjectId()), [_item(1)])
    assert exc.value.status_code == 404


# --- The status written is the one the engine derives ---
async def test_full_delivery_marks_the_pass_delivered(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await _create(gp_id, [_item(30)])

    gp = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert gp["status"] == "DELIVERED"


async def test_partial_delivery_marks_the_pass_partially_delivered(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await _create(gp_id, [_item(10)])

    gp = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert gp["status"] == "PARTIALLY_DELIVERED"


async def test_delivering_everything_back_again_does_not_close_the_pass(mocked_db):
    """Returned pieces mean the pass is not settled, whatever the arithmetic."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await seed_return(mocked_db, gp_id=gp_id, qty=5)

    await _create(gp_id, [_item(30)])

    gp = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    # 30 delivered but 5 came back, so 5 are outstanding and it cannot be DELIVERED.
    assert gp["status"] == "PARTIALLY_DELIVERED"


async def test_a_credit_correction_can_stop_a_full_delivery_closing(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await seed_adjustment(mocked_db, gp_id=gp_id, qty=4)

    await _create(gp_id, [_item(30)])

    gp = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    # 4 pieces were lost and are owed back, so this is not settled.
    assert gp["status"] == "PARTIALLY_DELIVERED"


# --- The derived status agrees with the balance the UI shows ---
async def test_written_status_matches_the_engine(mocked_db):
    gp_id = await seed_gp(
        mocked_db,
        items=[_gp_item(name="Pillow", received=30), _gp_item(name="Sheet", received=20)],
    )
    await seed_delivery(mocked_db, gp_id=gp_id, qty=10)

    await _create(gp_id, [_item(20), _item(5, name="Sheet")])

    gp = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    balance = await gp_mod.get_gate_pass_balance(gp_id, auth_user())
    # Pillow is settled, Sheet is not: the pass cannot read DELIVERED.
    assert balance["totals"]["outstanding_delivery_qty"] == 15
    assert gp["status"] == "PARTIALLY_DELIVERED"


async def test_a_return_already_resent_is_not_offered_again(mocked_db):
    """Re-sent pieces are back in the delivered column; offering them twice
    would let one physical delivery be recorded twice."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20)
    await seed_return(mocked_db, gp_id=gp_id, qty=10, resend_status="SENT")

    with pytest.raises(HTTPException) as exc:
        await _create(gp_id, [_item(11)])
    assert exc.value.status_code == 400
    assert "Only 10 available" in exc.value.detail

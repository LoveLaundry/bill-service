"""A return and an approved correction both move a balance. Guard both.

A return is one of the three terms in ``outstanding = received - delivered +
returned + correction``, so an unvalidated return manufactures pieces that were
never involved. An approved received-quantity correction re-derives the pass
status, and it used to re-derive that status without the pass's posted balance
corrections — so a pass that still owed a credited piece came back labelled
DELIVERED and vanished from the delivery form.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import encrypt_dict
from bill_service.gatepass_balance import load_gate_pass_balance_context
from bill_service.models import (
    BalanceAdjustmentCreate,
    GatePassAdjustmentRequest,
    ReturnCreate,
    ReturnItem,
    ReturnUpdate,
)
from bill_service.routers import adjustments as adjustments_router
from bill_service.routers import balance_adjustments as ba
from bill_service.routers import returns as returns_router
from bill_service.services import balance_engine as be

GP_SENSITIVE = ["client_name", "items", "notes"]


def _now():
    return datetime.now(timezone.utc)


def _item(*, name="Towel", spec=None, client=20, received=20) -> dict:
    return {
        "item_name": name,
        "category": "Bed",
        "specification": spec,
        "client_qty": client,
        "received_qty": received,
        "difference": received - client,
    }


def _user(name="Alice"):
    return {"auth_id": f"u-{name}", "user_name": name, "role": "ADMIN"}


class _Req:
    headers: dict = {}


async def seed_gp(mocked_db, *, items, client="Hotel A", status="RECEIVED") -> str:
    doc = {
        "gate_pass_number": f"GP-{ObjectId()}",
        "client_name": client,
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


async def seed_delivery(mocked_db, *, gp_id, qty, name="Towel", spec=None, client="Hotel A") -> str:
    doc = {
        "gate_pass_id": gp_id,
        "client_name": client,
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


# ── Returns must be for pieces the pass actually carried ─────────────────────


async def test_a_return_for_an_item_the_pass_never_had_is_refused(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(name="Towel")])

    with pytest.raises(HTTPException) as exc:
        await returns_router.create_return(
            ReturnCreate(
                gate_pass_id=gp_id,
                client_name="Hotel A",
                items=[
                    ReturnItem(
                        item_name="Curtain",
                        specification=None,
                        returned_qty=4,
                        reason="DAMAGED",
                        action="RECEIVE_BACK",
                    )
                ],
            ),
            _user(),
        )
    assert exc.value.status_code == 400
    assert "Curtain" in exc.value.detail
    assert await mocked_db["returns_collection"].count_documents({}) == 0


async def test_a_return_for_a_real_item_is_accepted_and_raises_the_balance(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(name="Towel", received=20, client=20)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20)

    await returns_router.create_return(
        ReturnCreate(
            gate_pass_id=gp_id,
            client_name="Hotel A",
            items=[
                ReturnItem(
                    item_name="Towel", specification=None, returned_qty=6,
                    reason="DAMAGED", action="RECEIVE_BACK"
                )
            ],
        ),
        _user(),
    )

    ctx = await load_gate_pass_balance_context(gp_id)
    assert ctx.balance["items"][be.item_key("Towel", None)]["outstanding_delivery_qty"] == 6
    # A return puts pieces back, so the pass must become deliverable again.
    raw = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert raw["status"] == "PARTIALLY_DELIVERED"


async def test_a_specification_mismatch_is_refused(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(name="Towel", spec="Large")])
    with pytest.raises(HTTPException) as exc:
        await returns_router.create_return(
            ReturnCreate(
                gate_pass_id=gp_id,
                client_name="Hotel A",
                items=[
                    ReturnItem(
                        item_name="Towel", specification="Small", returned_qty=2,
                        reason="DAMAGED", action="RECEIVE_BACK",
                    )
                ],
            ),
            _user(),
        )
    assert exc.value.status_code == 400


async def test_a_return_cannot_be_pointed_at_another_pass_delivery(mocked_db):
    """Crediting pieces to a pass that never sent them invents an outstanding."""
    mine = await seed_gp(mocked_db, items=[_item()], client="Hotel A")
    theirs = await seed_gp(mocked_db, items=[_item()], client="Hotel B")
    their_delivery = await seed_delivery(
        mocked_db, gp_id=theirs, qty=5, client="Hotel B"
    )

    with pytest.raises(HTTPException) as exc:
        await returns_router.create_return(
            ReturnCreate(
                gate_pass_id=mine,
                client_name="Hotel A",
                delivery_id=their_delivery,
                items=[
                    ReturnItem(
                        item_name="Towel", specification=None, returned_qty=3,
                        reason="DAMAGED", action="RECEIVE_BACK",
                    )
                ],
            ),
            _user(),
        )
    assert exc.value.status_code == 400
    assert "different gate pass" in exc.value.detail
    assert await mocked_db["returns_collection"].count_documents({}) == 0


async def test_editing_a_return_cannot_smuggle_in_an_unknown_item(mocked_db):
    """The edit path was the way around the create-time check."""
    gp_id = await seed_gp(mocked_db, items=[_item(name="Towel")])
    created = await returns_router.create_return(
        ReturnCreate(
            gate_pass_id=gp_id,
            client_name="Hotel A",
            items=[
                ReturnItem(
                    item_name="Towel", specification=None, returned_qty=2,
                    reason="DAMAGED", action="RECEIVE_BACK"
                )
            ],
        ),
        _user(),
    )

    with pytest.raises(HTTPException) as exc:
        await returns_router.update_return(
            created["return_id"],
            ReturnUpdate(
                items=[
                    ReturnItem(
                        item_name="Curtain", specification=None, returned_qty=9,
                        reason="DAMAGED", action="RECEIVE_BACK",
                    )
                ]
            ),
            _user(),
        )
    assert exc.value.status_code == 400

    # The stored return is untouched.
    stored = await returns_router.get_return(created["return_id"], _user())
    assert [i["item_name"] for i in stored["items"]] == ["Towel"]


# ── The approved-correction status re-derivation must see the corrections ────


async def test_approving_a_received_correction_keeps_a_credit_applied(mocked_db):
    """A credit posted first, then a received-qty correction approved.

    The re-derived status used to ignore the credit, so the pass went back to
    DELIVERED with pieces genuinely still owed — invisible to the delivery form.
    """
    gp_id = await seed_gp(mocked_db, items=[_item(received=20, client=20)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20)

    # Credit 6 pieces: the pass owes them again and reopens.
    await ba.post_balance_adjustment(
        BalanceAdjustmentCreate(
            gate_pass_id=gp_id, item_name="Towel", quantity=6,
            reason="PIECES_MISSING_IN_TRANSIT",
        ),
        _Req(),
        _user(),
    )
    raw = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert raw["status"] == "PARTIALLY_DELIVERED"

    # Now an approved received-quantity correction: 20 received -> 18.
    request = await adjustments_router.create_adjustment_route(
        GatePassAdjustmentRequest(
            gate_pass_id=gp_id, item_name="Towel", corrected_qty=18,
            reason="COUNTING_ERROR",
        ),
        _user("Alice"),
    )
    await adjustments_router.approve_adjustment(request["id"], _user("Bob"))

    ctx = await load_gate_pass_balance_context(gp_id)
    # 18 received - 20 recorded as sent + the 6-piece credit still applied.
    assert ctx.balance["items"][be.item_key("Towel", None)]["outstanding_delivery_qty"] == 4
    raw = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert raw["status"] == "PARTIALLY_DELIVERED", (
        "a credited piece is still owed, so the pass must stay deliverable"
    )


async def test_approving_a_correction_that_settles_the_pass_closes_it(mocked_db):
    """The control: with no credit outstanding, the pass really is delivered."""
    gp_id = await seed_gp(mocked_db, items=[_item(received=20, client=20)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20)

    from bill_service.models import GatePassAdjustmentRequest

    request = await adjustments_router.create_adjustment_route(
        GatePassAdjustmentRequest(
            gate_pass_id=gp_id, item_name="Towel", corrected_qty=20, reason="COUNTING_ERROR"
        ),
        _user("Alice"),
    )
    await adjustments_router.approve_adjustment(request["id"], _user("Bob"))

    raw = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert raw["status"] == "DELIVERED"


async def test_an_approved_correction_is_enqueued_for_replication(mocked_db):
    """MAIN was corrected while SECONDARY kept the old quantities and status."""
    gp_id = await seed_gp(mocked_db, items=[_item(received=20, client=20)])
    request = await adjustments_router.create_adjustment_route(
        GatePassAdjustmentRequest(
            gate_pass_id=gp_id, item_name="Towel", corrected_qty=12, reason="COUNTING_ERROR"
        ),
        _user("Alice"),
    )
    await adjustments_router.approve_adjustment(request["id"], _user("Bob"))

    queued = await mocked_db["sync_queue_collection"].find_one(
        {"entity": "gatepass", "record_id": gp_id}
    )
    assert queued is not None, "the corrected pass must be enqueued for the replica"
    assert queued["status"] == "PENDING"

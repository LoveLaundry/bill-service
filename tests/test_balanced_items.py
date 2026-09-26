"""Balanced items: returns and counting corrections must reopen a pass.

A pass that was fully delivered and is then balanced off — a piece comes back, a
counting mistake is corrected — is owed pieces again. The three consequences
covered here used to be missing, and together they made such a pass invisible:

  1. the stored status stayed DELIVERED, so nothing flagged it as still owing;
  2. ``GET /deliveries/pending-gatepasses`` excluded DELIVERED, so the pass
     never appeared in the delivery form to be selected;
  3. nothing recomputed the status when the return was written at all.
"""
from datetime import datetime, timezone

from bson import ObjectId

from bill_service.crypto_helper import encrypt_dict
from bill_service.routers import deliveries as dl
from bill_service.routers import returns as rt
from bill_service.services import balance_engine as be

from conftest import auth_user

GP_SENSITIVE = ["client_name", "items", "notes"]


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
    }
    res = await mocked_db["gatepasses_collection"].insert_one(encrypt_dict(doc, GP_SENSITIVE))
    return str(res.inserted_id)


async def seed_delivery(mocked_db, *, gp_id: str, qty: int, name="Pillow", spec=None) -> None:
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
    await mocked_db["deliveries_collection"].insert_one(encrypt_dict(doc, GP_SENSITIVE))


def _return_item(name="Pillow", spec=None, qty=4, action="RECEIVE_BACK"):
    from bill_service.models import ReturnItem

    return ReturnItem(
        item_name=name,
        specification=spec,
        returned_qty=qty,
        reason="DAMAGED",
        action=action,
    )


async def _post_return(gp_id: str, *items, **overrides):
    from bill_service.models import ReturnCreate

    payload = ReturnCreate(
        gate_pass_id=gp_id,
        client_name="Test Client",
        items=list(items),
        **overrides,
    )
    return await rt.create_return(payload, auth_user())

async def _status_of(mocked_db, gp_id: str) -> str:
    gp = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    return gp["status"]


async def _pending(mocked_db, client_name=None):
    return await dl.pending_gatepasses(client_name=client_name, current_user=auth_user())


# --- A return reopens a closed pass ---
async def test_a_return_reopens_a_fully_delivered_pass(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, qty=30)

    await _post_return(gp_id, _return_item(qty=4))

    # 30 sent, 4 handed back: the client is owed 4 again.
    assert await _status_of(mocked_db, gp_id) == "PARTIALLY_DELIVERED"


async def test_a_return_on_a_never_delivered_pass_stays_a_workflow_state(mocked_db):
    """A return is proof a send happened, so this one does read as partial."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)], status="READY_FOR_DELIVERY")

    await _post_return(gp_id, _return_item(qty=4))

    assert await _status_of(mocked_db, gp_id) == "PARTIALLY_DELIVERED"


async def test_a_short_receipt_alone_is_not_a_partial_delivery(mocked_db):
    """Counted 8 of 10 sent, nothing delivered: the pass is still at receiving.

    The 2 missing pieces are a receipt discrepancy, not a delivery, so calling
    this PARTIALLY_DELIVERED would claim a send that never happened.
    """
    gp_id = await seed_gp(
        mocked_db, items=[_gp_item(client=10, received=8)], status="RECEIVED"
    )

    # Nothing wrote a movement, so the status is untouched by construction.
    assert await _status_of(mocked_db, gp_id) == "RECEIVED"
    balance = be.compute_gate_pass_balance([_gp_item(client=10, received=8)], {})
    assert be.derive_gate_pass_status(balance, "RECEIVED") == "RECEIVED"


# --- ...and the reopened pass is selectable for delivery again ---
async def test_a_reopened_pass_appears_in_pending_gate_passes(mocked_db):
    """This is the failure the whole flow existed to prevent.

    The endpoint used to filter out DELIVERED passes, so a balanced pass was
    offered to nobody: the client was owed pieces and no screen could send them.
    """
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, qty=30)

    assert await _pending(mocked_db) == []

    await _post_return(gp_id, _return_item(qty=4))

    pending = await _pending(mocked_db)
    assert [p["gate_pass_id"] for p in pending] == [gp_id]
    assert pending[0]["total_pending"] == 4
    assert pending[0]["status"] == "PARTIALLY_DELIVERED"
    assert pending[0]["items"][0]["returned_qty"] == 4


async def test_pending_gate_passes_reports_the_balanced_amount(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, qty=28)
    await mocked_db["balance_adjustments_collection"].insert_one(
        {
            "gate_pass_id": gp_id,
            "delivery_id": None,
            "item_name": "Pillow",
            "specification": "",
            "quantity": 2,
            "reason": "PIECES_MISSING_IN_TRANSIT",
            "notes": None,
            "status": "POSTED",
            "created_by": "Alice",
            "created_at": _now(),
        }
    )

    pending = await _pending(mocked_db)
    assert pending[0]["total_pending"] == 4
    assert pending[0]["total_balance_adjusted"] == 2
    assert pending[0]["items"][0]["balance_adjustment_qty"] == 2


async def test_a_settled_pass_is_still_not_offered(mocked_db):
    """Dropping the status filter must not turn the list into every pass."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, qty=30)

    assert await _pending(mocked_db) == []


async def test_a_cancelled_pass_is_never_offered(mocked_db):
    gp_id = await seed_gp(
        mocked_db, items=[_gp_item(received=30)], status="CANCELLED"
    )
    await seed_delivery(mocked_db, gp_id=gp_id, qty=10)

    assert await _pending(mocked_db) == []


async def test_a_pass_with_nothing_received_is_never_offered(mocked_db):
    await seed_gp(mocked_db, items=[_gp_item(client=10, received=0)])

    assert await _pending(mocked_db) == []


async def test_a_legacy_note_closure_stays_hidden(mocked_db):
    """``marked_delivered`` reads as fully sent, so nothing is outstanding."""
    gp_id = await seed_gp(
        mocked_db, items=[_gp_item(received=30)], status="DELIVERED"
    )
    await mocked_db["gatepasses_collection"].update_one(
        {"_id": ObjectId(gp_id)}, {"$set": {"marked_delivered": True}}
    )

    assert await _pending(mocked_db) == []


# --- The re-send closes it again ---
async def test_resending_the_last_piece_closes_the_pass(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, qty=30)
    created = await _post_return(gp_id, _return_item(qty=4))
    return_id = created["return_id"]

    await rt.mark_item_resent(return_id, "Pillow", "", auth_user())

    assert await _status_of(mocked_db, gp_id) == "DELIVERED"
    assert await _pending(mocked_db) == []


async def test_editing_a_return_quantity_re_derives_the_pass(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)], status="DELIVERED")
    await seed_delivery(mocked_db, gp_id=gp_id, qty=30)
    created = await _post_return(gp_id, _return_item(qty=4))

    from bill_service.models import ReturnUpdate

    await rt.update_return(
        created["return_id"],
        ReturnUpdate(items=[_return_item(action="DISCARD").model_dump()]),
        auth_user(),
    )

    # A discarded return is never re-sent, so nothing is pending and the pass is
    # settled again. Without re-deriving on edit it would be stuck open.
    assert await _status_of(mocked_db, gp_id) == "DELIVERED"


# --- The engine rule itself ---
def test_a_credit_counts_as_proof_of_a_send():
    """A credit is only ever posted against a send recorded short."""
    balance = be.compute_gate_pass_balance(
        [_gp_item(received=30)], {"Pillow||": 30}, balance_adjustment_by_item={"Pillow||": 3}
    )
    assert balance["totals"]["outstanding_delivery_qty"] == 3
    assert be.derive_gate_pass_status(balance, "DELIVERED") == "PARTIALLY_DELIVERED"


def test_a_debit_is_not_proof_of_a_send():
    """A debit means we logged more as sent than we took, so nothing went out."""
    balance = be.compute_gate_pass_balance(
        [_gp_item(received=30)], {"Pillow||": 0}, balance_adjustment_by_item={"Pillow||": -5}
    )
    assert balance["totals"]["outstanding_delivery_qty"] == 25
    assert be.derive_gate_pass_status(balance, "READY_FOR_DELIVERY") == "READY_FOR_DELIVERY"


def test_a_resent_return_is_not_proof_of_a_send():
    balance = be.compute_gate_pass_balance(
        [_gp_item(received=30)],
        {"Pillow||": 30},
        {"Pillow||": 0},
    )
    assert be.derive_gate_pass_status(balance, "DELIVERED") == "DELIVERED"

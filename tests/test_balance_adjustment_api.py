"""Tests for the balance-adjustment endpoints and the delivery balance report.

Covers the request contract (a reason is mandatory, a zero correction is
rejected, the item must exist on the pass), the immediate-apply behaviour, the
void path, and the printed report that the delivery note renders.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from bill_service.crypto_helper import encrypt_dict
from bill_service.models import BalanceAdjustmentCreate
from bill_service.routers import balance_adjustments as ba

from conftest import auth_user

GP_SENSITIVE = ["client_name", "items", "notes"]
DL_SENSITIVE = ["client_name", "items", "notes"]


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
    res = await mocked_db["deliveries_collection"].insert_one(encrypt_dict(doc, DL_SENSITIVE))
    return str(res.inserted_id)


def _payload(gate_pass_id: str, **overrides) -> BalanceAdjustmentCreate:
    base = {
        "item_name": "Pillow",
        "specification": None,
        "quantity": 3,
        "reason": "PIECES_MISSING_IN_TRANSIT",
        "gate_pass_id": gate_pass_id,
    }
    base.update(overrides)
    return BalanceAdjustmentCreate(**base)


# --- Request contract ---
def test_reason_is_mandatory():
    with pytest.raises(ValidationError):
        BalanceAdjustmentCreate(item_name="Pillow", quantity=3, gate_pass_id="x", reason="")


def test_zero_quantity_is_rejected():
    """A zero correction is a no-op; omitting it is clearer than posting one."""
    with pytest.raises(ValidationError):
        BalanceAdjustmentCreate(item_name="Pillow", quantity=0, reason="R", gate_pass_id="x")


def test_negative_quantity_is_allowed():
    adj = BalanceAdjustmentCreate(
        item_name="Pillow", quantity=-4, reason="R", gate_pass_id="x"
    )
    assert adj.quantity == -4


# --- Posting ---
async def test_post_credit_applies_immediately(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item()])
    dl_id = await seed_delivery(mocked_db, gp_id=gp_id, qty=30)

    out = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=3, delivery_id=dl_id), _req(), auth_user()
    )
    assert out["status"] == "POSTED"
    assert out["quantity"] == 3
    assert out["delivery_id"] == dl_id
    assert out["created_by"] == "Alice"

    stored = await mocked_db["balance_adjustments_collection"].find_one(
        {"_id": ObjectId(out["id"])}
    )
    assert stored["quantity"] == 3


async def test_post_credit_reopens_a_closed_gate_pass(mocked_db):
    """Crediting a client on a DELIVERED pass must stop it reading DELIVERED."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item()], status="DELIVERED")
    dl_id = await seed_delivery(mocked_db, gp_id=gp_id, qty=30)

    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=2, delivery_id=dl_id), _req(), auth_user()
    )

    gp = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert gp["status"] == "PARTIALLY_DELIVERED"


async def test_post_debit_can_close_an_open_balance(mocked_db):
    """A negative correction squares off a pass we over-recorded sending."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20)

    # 30 received - 20 sent = 10 outstanding; a -10 debit squares it off.
    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=-10), _req(), auth_user()
    )

    gp = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert gp["status"] == "DELIVERED"


async def test_post_rejects_an_item_not_on_the_gate_pass(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item()])
    with pytest.raises(HTTPException) as exc:
        await ba.post_balance_adjustment(
            _payload(gp_id, item_name="Curtain"), _req(), auth_user()
        )
    assert exc.value.status_code == 404


async def test_post_rejects_a_delivery_from_another_gate_pass(mocked_db):
    gp_a = await seed_gp(mocked_db, items=[_gp_item()])
    gp_b = await seed_gp(mocked_db, items=[_gp_item()])
    dl_id = await seed_delivery(mocked_db, gp_id=gp_b, qty=10)

    with pytest.raises(HTTPException) as exc:
        await ba.post_balance_adjustment(
            _payload(gp_a, delivery_id=dl_id), _req(), auth_user()
        )
    assert exc.value.status_code == 400


async def test_post_rejects_a_cancelled_gate_pass(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item()], status="CANCELLED")
    with pytest.raises(HTTPException) as exc:
        await ba.post_balance_adjustment(_payload(gp_id), _req(), auth_user())
    assert exc.value.status_code == 409


async def test_post_rejects_an_unknown_gate_pass(mocked_db):
    with pytest.raises(HTTPException) as exc:
        await ba.post_balance_adjustment(
            _payload(str(ObjectId())), _req(), auth_user()
        )
    assert exc.value.status_code == 404


# --- Void ---
async def test_void_removes_the_effect(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item()])
    dl_id = await seed_delivery(mocked_db, gp_id=gp_id, qty=30)

    posted = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=4, delivery_id=dl_id), _req(), auth_user()
    )
    await ba.void_balance_adjustment(
        posted["id"], "Entered against the wrong gate pass", auth_user("Bob")
    )

    listed = await ba.list_balance_adjustments(gp_id, None, None, auth_user())
    assert listed[0]["status"] == "VOID"

    report = await ba.delivery_balance_report(dl_id, auth_user())
    assert report["items"][0]["balance_adjustment_qty"] == 0
    assert report["items"][0]["current_balance_qty"] == 0


async def test_void_recloses_a_pass_the_credit_had_reopened(mocked_db):
    """The reversal path has to put the status back, or the pass sticks open."""
    gp_id = await seed_gp(mocked_db, items=[_gp_item()], status="DELIVERED")
    dl_id = await seed_delivery(mocked_db, gp_id=gp_id, qty=30)

    posted = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=3, delivery_id=dl_id), _req(), auth_user()
    )
    reopened = await mocked_db["gatepasses_collection"].find_one(
        {"_id": ObjectId(gp_id)}
    )
    assert reopened["status"] == "PARTIALLY_DELIVERED"

    await ba.void_balance_adjustment(posted["id"], "Posted in error", auth_user("Bob"))

    reclosed = await mocked_db["gatepasses_collection"].find_one(
        {"_id": ObjectId(gp_id)}
    )
    assert reclosed["status"] == "DELIVERED"


async def test_void_reopens_a_pass_the_debit_had_closed(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20)

    posted = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=-10), _req(), auth_user()
    )
    closed = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    assert closed["status"] == "DELIVERED"

    await ba.void_balance_adjustment(posted["id"], "Wrong reason code", auth_user("Bob"))

    reopened = await mocked_db["gatepasses_collection"].find_one(
        {"_id": ObjectId(gp_id)}
    )
    assert reopened["status"] == "PARTIALLY_DELIVERED"


async def test_void_of_one_correction_leaves_the_others_applied(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item()])
    dl_id = await seed_delivery(mocked_db, gp_id=gp_id, qty=30)

    keep = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=2, delivery_id=dl_id), _req(), auth_user()
    )
    drop = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=5, delivery_id=dl_id), _req(), auth_user()
    )
    await ba.void_balance_adjustment(drop["id"], "Duplicate", auth_user("Bob"))

    report = await ba.delivery_balance_report(dl_id, auth_user())
    row = report["items"][0]
    assert row["balance_adjustment_qty"] == 2
    assert row["current_balance_qty"] == 2


async def test_void_twice_is_rejected(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item()])
    posted = await ba.post_balance_adjustment(_payload(gp_id), _req(), auth_user())
    await ba.void_balance_adjustment(posted["id"], "R", auth_user("Bob"))
    with pytest.raises(HTTPException) as exc:
        await ba.void_balance_adjustment(posted["id"], "R", auth_user("Bob"))
    assert exc.value.status_code == 409


# --- The printed report ---
async def test_report_shows_the_four_running_balance_figures(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item(received=30)])
    first = await seed_delivery(mocked_db, gp_id=gp_id, qty=10)
    second = await seed_delivery(mocked_db, gp_id=gp_id, qty=15)

    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=2, delivery_id=second), _req(), auth_user()
    )

    report = await ba.delivery_balance_report(second, auth_user())
    assert report["gate_pass_id"] == gp_id
    row = report["items"][0]
    assert row["previous_balance_qty"] == 20  # 30 received, 10 already sent
    assert row["received_qty"] == 30
    assert row["delivered_qty"] == 15
    assert row["balance_adjustment_qty"] == 2
    assert row["current_balance_qty"] == 7  # 20 - 15 + 2
    assert row["reconciles"] is True

    # The earlier note is unaffected by a later delivery's correction.
    earlier = await ba.delivery_balance_report(first, auth_user())
    assert earlier["items"][0]["current_balance_qty"] == 20


async def test_report_rejects_a_cancelled_delivery(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_gp_item()])
    doc = {
        "gate_pass_id": gp_id,
        "client_name": "Test Client",
        "delivery_date": _now(),
        "delivered_by": "R",
        "received_by": "C",
        "items": [{"item_name": "Pillow", "specification": None, "quantity": 5}],
        "status": "CANCELLED",
        "notes": "",
        "created_at": _now(),
    }
    res = await mocked_db["deliveries_collection"].insert_one(encrypt_dict(doc, DL_SENSITIVE))
    with pytest.raises(HTTPException) as exc:
        await ba.delivery_balance_report(str(res.inserted_id), auth_user())
    assert exc.value.status_code == 409


async def test_report_404s_for_an_unknown_delivery(mocked_db):
    with pytest.raises(HTTPException) as exc:
        await ba.delivery_balance_report(str(ObjectId()), auth_user())
    assert exc.value.status_code == 404

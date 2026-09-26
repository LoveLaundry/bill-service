"""Regression tests for the balance-correction defects found in the audit.

Each test names the wrong behaviour it is locking down. They are grouped by the
surface that was wrong, not by the endpoint, because most of the bugs were a
figure disagreeing with another figure rather than a single call misbehaving.
"""
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import decrypt_dict, encrypt_dict
from bill_service.models import BalanceAdjustmentCreate
from bill_service.routers import balance_adjustments as ba
from bill_service.routers import dashboard, reconciliation
from bill_service.services import balance_engine as be
from bill_service.gatepass_balance import load_gate_pass_balance_context

from conftest import auth_user

GP_SENSITIVE = ["client_name", "items", "notes"]
DL_SENSITIVE = ["client_name", "items", "notes"]


class _Req:
    def __init__(self, key: str | None = None):
        self.headers = {"X-Idempotency-Key": key} if key else {}


def _now():
    return datetime.now(timezone.utc)


def _item(*, name="Pillow", spec=None, client=30, received=30) -> dict:
    return {
        "item_name": name,
        "category": "Bed",
        "specification": spec,
        "client_qty": client,
        "received_qty": received,
        "difference": received - client,
    }


async def seed_gp(
    mocked_db,
    *,
    items,
    client="Test Client",
    status="RECEIVED",
    marked_delivered=None,
    number=None,
) -> str:
    doc = {
        "gate_pass_number": number or f"GP-{ObjectId()}",
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
    if marked_delivered is not None:
        doc["marked_delivered"] = marked_delivered
    res = await mocked_db["gatepasses_collection"].insert_one(encrypt_dict(doc, GP_SENSITIVE))
    return str(res.inserted_id)


async def seed_delivery(
    mocked_db, *, gp_id: str, qty: int, name="Pillow", spec=None, when=None, client="Test Client"
) -> str:
    doc = {
        "gate_pass_id": gp_id,
        "client_name": client,
        "delivery_date": when or _now(),
        "delivered_by": "Rider",
        "received_by": "Client",
        "items": [{"item_name": name, "specification": spec, "quantity": qty}],
        "status": "DELIVERED",
        "notes": "",
        "created_at": _now(),
    }
    res = await mocked_db["deliveries_collection"].insert_one(encrypt_dict(doc, DL_SENSITIVE))
    return str(res.inserted_id)


async def seed_return(mocked_db, *, gp_id: str, name="Pillow", spec=None, qty=5) -> None:
    doc = {
        "return_id": f"RT-{ObjectId()}",
        "gate_pass_id": gp_id,
        "delivery_id": None,
        "client_name": "Test Client",
        "items": [
            {
                "item_name": name,
                "specification": spec,
                "returned_qty": qty,
                "action": "RECEIVE_BACK",
                "resend_status": "PENDING",
            }
        ],
        "status": "PENDING",
        "created_at": _now(),
        "updated_at": _now(),
    }
    await mocked_db["returns_collection"].insert_one(encrypt_dict(doc, GP_SENSITIVE))


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


async def outstanding_for(mocked_db, gp_id: str, name="Pillow", spec=None) -> int:
    ctx = await load_gate_pass_balance_context(gp_id)
    return ctx.balance["items"][be.item_key(name, spec)]["outstanding_delivery_qty"]


async def status_of(mocked_db, gp_id: str) -> str:
    doc = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    return doc["status"]


# ── Idempotency: a repeated correction must not be swallowed ──────────────────
#
# The client used to derive X-Idempotency-Key from a hash of the payload, so two
# genuinely separate corrections that happened to be identical were the same key
# and the second returned the first. The balance could then never be corrected
# twice from the UI no matter how many times the operator tried.


async def test_two_identical_corrections_are_two_corrections(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item()])

    first = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=3, notes="batch 1"), _Req("key-one"), auth_user()
    )
    second = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=3, notes="batch 2"), _Req("key-two"), auth_user()
    )

    assert first["id"] != second["id"]
    listed = await ba.list_balance_adjustments(gp_id, None, None, auth_user())
    assert len(listed) == 2
    assert await outstanding_for(mocked_db, gp_id) == 36


async def test_a_retried_submission_still_returns_the_first_correction(mocked_db):
    """The guarantee that unique keys must not cost: one action, one record."""
    gp_id = await seed_gp(mocked_db, items=[_item()])

    first = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=3), _Req("same-key"), auth_user()
    )
    replay = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=3), _Req("same-key"), auth_user()
    )

    assert replay["id"] == first["id"]
    assert len(await ba.list_balance_adjustments(gp_id, None, None, auth_user())) == 1
    assert await outstanding_for(mocked_db, gp_id) == 33


# ── A debit that can never move anything is refused, not silently recorded ───


async def test_a_debit_against_a_zero_balance_is_refused(mocked_db):
    """Recording it would show a correction on the note that changes nothing."""
    gp_id = await seed_gp(mocked_db, items=[_item(received=10, client=10)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=10)
    assert await outstanding_for(mocked_db, gp_id) == 0

    with pytest.raises(HTTPException) as exc:
        await ba.post_balance_adjustment(
            _payload(gp_id, quantity=-3, reason="OVER_RECORDED_DELIVERY"), _Req(), auth_user()
        )
    assert exc.value.status_code == 409
    assert "no outstanding balance" in exc.value.detail.lower()
    assert await ba.list_balance_adjustments(gp_id, None, None, auth_user()) == []


async def test_a_debit_within_the_balance_is_still_accepted(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(received=10, client=10)])
    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=-4, reason="OVER_RECORDED_DELIVERY"), _Req(), auth_user()
    )
    assert await outstanding_for(mocked_db, gp_id) == 6


# ── The debit must not be able to drive the balance negative ─────────────────


async def test_debits_cannot_take_the_balance_below_zero(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(received=10, client=10)])
    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=-10, reason="OVER_RECORDED_DELIVERY"), _Req(), auth_user()
    )
    assert await outstanding_for(mocked_db, gp_id) == 0
    assert "BALANCE_DEBITED" in (
        (await load_gate_pass_balance_context(gp_id)).balance
        ["items"][be.item_key("Pillow", None)]["flags"]
    )


# ── A credit on one item is proof of a prior send even when another is debited ─
#
# has_prior_send used to sum the adjustments across the whole pass, so a debit on
# one item cancelled a credit on another and the pass went back to reading
# RECEIVED — disappearing from the delivery form with pieces still owed.


async def test_a_credit_on_one_item_survives_a_debit_on_another(mocked_db):
    items = [_item(name="Pillow"), _item(name="Sheet")]
    gp_id = await seed_gp(mocked_db, items=items)

    await ba.post_balance_adjustment(
        _payload(gp_id, item_name="Pillow", quantity=4), _Req(), auth_user()
    )
    await ba.post_balance_adjustment(
        _payload(gp_id, item_name="Sheet", quantity=-2, reason="OVER_RECORDED_DELIVERY"),
        _Req(),
        auth_user(),
    )

    balance = (await load_gate_pass_balance_context(gp_id)).balance
    # Pass-wide total is +2, but the credit on Pillow is real proof of a send.
    assert balance["totals"]["balance_adjustment_qty"] == 2
    assert await status_of(mocked_db, gp_id) == "PARTIALLY_DELIVERED"


# ── Legacy note closure: one answer, everywhere ──────────────────────────────
#
# pending-gatepasses honoured the legacy flag and the shared loader ignored it,
# so the delivery form hid a legacy pass as settled while the balance screen and
# the delivery endpoint both said 20 pieces were still owed.


async def test_a_legacy_closed_pass_reads_the_same_on_every_surface(mocked_db):
    gp_id = await seed_gp(
        mocked_db,
        items=[_item(received=20, client=20)],
        status="DELIVERED",
        marked_delivered={"note": "sent by rider", "at": _now()},
    )
    ctx = await load_gate_pass_balance_context(gp_id)
    assert ctx.balance["totals"]["outstanding_delivery_qty"] == 0
    assert "MARKED_DELIVERED_LEGACY" in ctx.balance["flags"]


async def test_a_legacy_closure_can_still_be_corrected(mocked_db):
    """A hidden balance is not a sealed one — an operator can still square it off."""
    gp_id = await seed_gp(
        mocked_db,
        items=[_item(received=20, client=20)],
        status="DELIVERED",
        marked_delivered={"note": "sent by rider", "at": _now()},
    )
    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=5), _Req(), auth_user()
    )
    assert await outstanding_for(mocked_db, gp_id) == 5
    assert await status_of(mocked_db, gp_id) == "PARTIALLY_DELIVERED"


# ── The delivery note must not disagree with the gate-pass balance ────────────


async def test_two_batches_of_the_same_item_are_summed_on_the_note(mocked_db):
    """Two rows for one item used to make the note print the LAST batch only."""
    gp_id = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=8), _item(name="Towel", received=12)]
    )
    d1 = await seed_delivery(mocked_db, gp_id=gp_id, qty=10, name="Towel", when=_now() - timedelta(days=2))
    d2 = await seed_delivery(mocked_db, gp_id=gp_id, qty=6, name="Towel", when=_now() - timedelta(days=1))

    report = await ba.delivery_balance_report(d2, auth_user())
    row = report["items"][0]
    assert row["received_qty"] == 20
    assert row["previous_balance_qty"] == 10
    assert row["current_balance_qty"] == 4
    assert report["totals"]["current_balance_qty"] == 4
    assert d1  # keeps the earlier delivery referenced


async def test_the_last_note_prints_the_real_gate_pass_balance(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(received=20, client=20)])
    d1 = await seed_delivery(mocked_db, gp_id=gp_id, qty=8, when=_now() - timedelta(days=2))
    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=4, delivery_id=d1), _Req(), auth_user()
    )
    d2 = await seed_delivery(mocked_db, gp_id=gp_id, qty=5, when=_now() - timedelta(days=1))

    report = await ba.delivery_balance_report(d2, auth_user())
    assert report["items"][0]["current_balance_qty"] == await outstanding_for(mocked_db, gp_id)
    assert report["items"][0]["reconciles"] is True


async def test_the_note_flags_a_balance_it_cannot_reconcile(mocked_db):
    """reconciles used to compare a number with itself and always answer yes."""
    gp_id = await seed_gp(mocked_db, items=[_item(received=5, client=5)])
    # 12 recorded as sent against 5 ever received: a real data error.
    await seed_delivery(mocked_db, gp_id=gp_id, qty=12, when=_now() - timedelta(days=2))
    d2 = await seed_delivery(mocked_db, gp_id=gp_id, qty=1, when=_now() - timedelta(days=1))
    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=3, delivery_id=d2), _Req(), auth_user()
    )

    report = await ba.delivery_balance_report(d2, auth_user())
    row = report["items"][0]
    assert "OVER_DELIVERED_BEFORE_DELIVERY" in row["flags"]
    assert row["reconciles"] is False
    # And the printed figure is still the engine's truth, not a made-up one.
    assert row["current_balance_qty"] == await outstanding_for(mocked_db, gp_id)


async def test_the_report_survives_a_legacy_string_delivery_date(mocked_db):
    """A hand-seeded row with an ISO string used to raise TypeError -> 500."""
    gp_id = await seed_gp(mocked_db, items=[_item(received=10, client=10)])
    d1 = await seed_delivery(mocked_db, gp_id=gp_id, qty=4)
    d2 = await seed_delivery(mocked_db, gp_id=gp_id, qty=2)
    await mocked_db["deliveries_collection"].update_one(
        {"_id": ObjectId(d1)}, {"$set": {"delivery_date": "2024-01-05"}}
    )

    report = await ba.delivery_balance_report(d2, auth_user())
    assert report["items"][0]["previous_balance_qty"] == 6


# ── Full lifecycle: post, edit by further correction, void, re-void ───────────


async def test_the_full_correction_lifecycle(mocked_db):
    items = [_item(received=30, client=30)]
    gp_id = await seed_gp(mocked_db, items=items, status="DELIVERED")
    d1 = await seed_delivery(mocked_db, gp_id=gp_id, qty=30, when=_now() - timedelta(days=1))
    assert await status_of(mocked_db, gp_id) == "DELIVERED"

    first = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=3, delivery_id=d1, notes="lost in transit"), _Req(), auth_user()
    )
    assert await outstanding_for(mocked_db, gp_id) == 3
    assert await status_of(mocked_db, gp_id) == "PARTIALLY_DELIVERED"

    # A second, different correction stacks on the first.
    second = await ba.post_balance_adjustment(
        _payload(gp_id, quantity=2, delivery_id=d1, reason="PIECES_DAMAGED_OR_SOILED"),
        _Req(),
        auth_user(),
    )
    assert await outstanding_for(mocked_db, gp_id) == 5

    # Voiding one leaves the other applied.
    await ba.void_balance_adjustment(first["id"], "RECORDING_ERROR", auth_user("Bob"))
    assert await outstanding_for(mocked_db, gp_id) == 2
    assert await status_of(mocked_db, gp_id) == "PARTIALLY_DELIVERED"

    # Voiding the last one closes the pass again.
    await ba.void_balance_adjustment(second["id"], "DUPLICATE", auth_user("Bob"))
    assert await outstanding_for(mocked_db, gp_id) == 0
    assert await status_of(mocked_db, gp_id) == "DELIVERED"

    # A re-void is refused, and the history survives.
    with pytest.raises(HTTPException) as exc:
        await ba.void_balance_adjustment(second["id"], "DUPLICATE", auth_user("Bob"))
    assert exc.value.status_code == 409
    listed = await ba.list_balance_adjustments(gp_id, None, None, auth_user())
    assert len(listed) == 2
    assert all(a["status"] == "VOID" for a in listed)


async def test_persisted_balance_survives_a_reload(mocked_db):
    """Refreshing / reopening must read exactly what was written."""
    gp_id = await seed_gp(mocked_db, items=[_item(received=25, client=25)])
    d1 = await seed_delivery(mocked_db, gp_id=gp_id, qty=10, when=_now() - timedelta(days=3))
    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=6, delivery_id=d1), _Req(), auth_user()
    )
    await ba.post_balance_adjustment(
        _payload(gp_id, quantity=-4, reason="OVER_RECORDED_DELIVERY", delivery_id=d1),
        _Req(),
        auth_user(),
    )

    expected = await outstanding_for(mocked_db, gp_id)
    assert expected == 17

    # A completely fresh read — no in-memory state carried over.
    reloaded = await load_gate_pass_balance_context(gp_id)
    assert reloaded.balance["items"][be.item_key("Pillow", None)]["outstanding_delivery_qty"] == 17
    report = await ba.delivery_balance_report(d1, auth_user())
    assert report["items"][0]["balance_adjustment_qty"] == 2


async def test_two_hotels_never_share_a_balance(mocked_db):
    hotel_a = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=20, client=20)], client="Hotel A"
    )
    hotel_b = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=50, client=50)], client="Hotel B"
    )
    await seed_delivery(mocked_db, gp_id=hotel_a, qty=5, name="Towel", client="Hotel A")
    await seed_delivery(mocked_db, gp_id=hotel_b, qty=50, name="Towel", client="Hotel B")

    await ba.post_balance_adjustment(
        _payload(hotel_a, item_name="Towel", quantity=7), _Req(), auth_user()
    )

    assert await outstanding_for(mocked_db, hotel_a, name="Towel") == 22
    assert await outstanding_for(mocked_db, hotel_b, name="Towel") == 0

    summary_a = await dashboard.get_client_summary("Hotel A")
    summary_b = await dashboard.get_client_summary("Hotel B")
    assert summary_a["stats"]["pending_items"] == 22
    assert summary_b["stats"]["pending_items"] == 0


# ── Returns reach every balance, including for un-specified items ────────────
#
# dashboard._key() returned a bare name for an un-specified item while the engine
# keys everything as name||spec, so for most items the returned quantity was
# looked up under a key that does not exist and contributed nothing.


async def test_an_unspecified_return_counts_towards_the_client_balance(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item(name="Towel", received=20, client=20)])
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20, name="Towel")
    await seed_return(mocked_db, gp_id=gp_id, name="Towel", qty=6)

    assert await outstanding_for(mocked_db, gp_id, name="Towel") == 6

    summary = await dashboard.get_client_summary("Test Client")
    assert summary["stats"]["pending_items"] == 6
    row = next(r for r in summary["pending_balances"] if r["item_name"] == "Towel")
    assert row["returned"] == 6
    assert row["pending"] == 6


async def test_another_clients_returns_never_reach_this_clients_balance(mocked_db):
    mine = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=20, client=20)], client="Hotel A"
    )
    theirs = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=20, client=20)], client="Hotel B"
    )
    await seed_delivery(mocked_db, gp_id=mine, qty=20, name="Towel", client="Hotel A")
    await seed_delivery(mocked_db, gp_id=theirs, qty=20, name="Towel", client="Hotel B")
    await seed_return(mocked_db, gp_id=theirs, name="Towel", qty=9)

    summary = await dashboard.get_client_summary("Hotel A")
    assert summary["stats"]["pending_items"] == 0
    row = next(r for r in summary["pending_balances"] if r["item_name"] == "Towel")
    assert row["returned"] == 0
    assert row["pending"] == 0


async def test_another_clients_corrections_never_reach_this_clients_balance(mocked_db):
    mine = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=20, client=20)], client="Hotel A"
    )
    theirs = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=20, client=20)], client="Hotel B"
    )
    await seed_delivery(mocked_db, gp_id=mine, qty=20, name="Towel", client="Hotel A")
    await seed_delivery(mocked_db, gp_id=theirs, qty=20, name="Towel", client="Hotel B")
    await ba.post_balance_adjustment(
        _payload(theirs, item_name="Towel", quantity=11), _Req(), auth_user()
    )

    summary = await dashboard.get_client_summary("Hotel A")
    row = next(r for r in summary["pending_balances"] if r["item_name"] == "Towel")
    assert row["balance_adjusted"] == 0
    assert row["pending"] == 0
    assert summary["stats"]["pending_items"] == 0


async def test_a_cancelled_pass_contributes_nothing_to_the_client_balance(mocked_db):
    live = await seed_gp(mocked_db, items=[_item(name="Towel", received=20, client=20)])
    dead = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=99, client=99)], status="CANCELLED"
    )
    await seed_delivery(mocked_db, gp_id=live, qty=20, name="Towel")

    summary = await dashboard.get_client_summary("Test Client")
    assert summary["stats"]["pending_items"] == 0
    assert summary["stats"]["total_items_received"] == 20
    assert dead  # the cancelled pass exists, it just must not move the numbers


async def test_reconciliation_reads_returns(mocked_db):
    """Returns were read raw, so `items` came back as ciphertext and counted zero.

    The pass below really does owe 6 pieces again, so reconciliation is right to
    flag it — the point is that it flags it for the RIGHT reason, with the
    returned quantity visible, rather than because the balance engine silently
    dropped the return.
    """
    gp_id = await seed_gp(
        mocked_db, items=[_item(name="Towel", received=20, client=20)], status="DELIVERED"
    )
    await seed_delivery(mocked_db, gp_id=gp_id, qty=20, name="Towel")
    await seed_return(mocked_db, gp_id=gp_id, name="Towel", qty=6)

    result = await reconciliation.reconciliation_issues(None, auth_user())
    assert result["unreadable_returns"] == 0
    flagged = next(i for i in result["items"] if i["id"] == gp_id)
    assert flagged["totals"]["returned_back_qty"] == 6
    assert flagged["totals"]["outstanding_delivery_qty"] == 6
    assert [i["code"] for i in flagged["issues"]] == ["CLOSED_WITH_OUTSTANDING"]

    # A closed pass with nothing returned and nothing owed is not flagged.
    control = await seed_gp(
        mocked_db, items=[_item(name="Sheet", received=20, client=20)], status="DELIVERED"
    )
    await seed_delivery(mocked_db, gp_id=control, qty=20, name="Sheet")
    result = await reconciliation.reconciliation_issues(None, auth_user())
    assert control not in {i["id"] for i in result["items"]}


# ── Engine invariants worth pinning ──────────────────────────────────────────


def test_merge_gate_pass_items_sums_duplicate_rows():
    merged = be.merge_gate_pass_items(
        [
            {"item_name": "Towel", "specification": None, "client_qty": 5, "received_qty": 5},
            {"item_name": "Towel", "specification": None, "client_qty": 7, "received_qty": 6},
        ]
    )
    assert len(merged) == 1
    assert merged[0]["received_qty"] == 11
    assert merged[0]["client_qty"] == 12


def test_order_deliveries_sorts_mixed_date_types():
    docs = [
        {"id": "a", "delivery_date": "2024-01-05"},
        {"id": "b", "delivery_date": datetime(2024, 1, 1, tzinfo=timezone.utc)},
        {"id": "c", "delivery_date": None, "created_at": datetime(2023, 12, 1, tzinfo=timezone.utc)},
    ]
    ordered = be.order_deliveries(docs, "a")
    assert [d["id"] for d in ordered] == ["c", "b", "a"]


def test_order_deliveries_appends_a_target_it_was_not_given():
    ordered = be.order_deliveries([{"id": "a"}], "zz")
    assert ordered[-1]["id"] == "zz"


async def test_a_delivery_is_capped_by_the_corrected_balance(mocked_db):
    """The engine's cap has to see corrections, or a credit lets over-delivery."""
    from bill_service.routers import deliveries as deliveries_router

    gp_id = await seed_gp(mocked_db, items=[_item(name="Towel", received=10, client=10)])
    await ba.post_balance_adjustment(
        _payload(gp_id, item_name="Towel", quantity=5), _Req(), auth_user()
    )

    from bill_service.models import DeliveryCreate, DeliveryItem

    ok = await deliveries_router.create_delivery(
        DeliveryCreate(
            gate_pass_id=gp_id,
            client_name="Test Client",
            delivery_date=_now(),
            delivered_by="Rider",
            received_by="Client",
            items=[DeliveryItem(item_name="Towel", quantity=15)],
        ),
        auth_user(),
        _Req(),
    )
    assert sum(i["quantity"] for i in ok["items"]) == 15

    with pytest.raises(HTTPException) as exc:
        await deliveries_router.create_delivery(
            DeliveryCreate(
                gate_pass_id=gp_id,
                client_name="Test Client",
                delivery_date=_now(),
                delivered_by="Rider",
                received_by="Client",
                items=[DeliveryItem(item_name="Towel", quantity=1)],
            ),
            auth_user(),
            _Req(),
        )
    assert exc.value.status_code == 400
    assert "Only 0 available" in exc.value.detail


async def test_a_correction_on_a_cancelled_delivery_is_refused(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item()])
    d1 = await seed_delivery(mocked_db, gp_id=gp_id, qty=5)
    await mocked_db["deliveries_collection"].update_one(
        {"_id": ObjectId(d1)}, {"$set": {"status": "CANCELLED"}}
    )
    with pytest.raises(HTTPException) as exc:
        await ba.post_balance_adjustment(
            _payload(gp_id, quantity=3, delivery_id=d1), _Req(), auth_user()
        )
    assert exc.value.status_code == 409


async def test_the_corrections_list_can_be_paged(mocked_db):
    gp_id = await seed_gp(mocked_db, items=[_item()])
    for n in range(5):
        await ba.post_balance_adjustment(
            _payload(gp_id, quantity=n + 1), _Req(f"page-{n}"), auth_user()
        )
    newest_first = await ba.list_balance_adjustments(
        gp_id, None, None, auth_user(), skip=0, limit=2
    )
    assert [a["quantity"] for a in newest_first] == [5, 4]
    third_page = await ba.list_balance_adjustments(
        gp_id, None, None, auth_user(), skip=2, limit=2
    )
    assert [a["quantity"] for a in third_page] == [3, 2]


async def test_gate_pass_decryption_still_round_trips_after_a_correction(mocked_db):
    """Guards the crypto path the correction writes flow through."""
    gp_id = await seed_gp(mocked_db, items=[_item()])
    await ba.post_balance_adjustment(_payload(gp_id, quantity=3), _Req(), auth_user())
    raw = await mocked_db["gatepasses_collection"].find_one({"_id": ObjectId(gp_id)})
    dec = decrypt_dict(raw, GP_SENSITIVE)
    assert dec["client_name"] == "Test Client"
    assert dec["items"][0]["item_name"] == "Pillow"

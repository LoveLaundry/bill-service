"""End-to-end delivery lifecycle: scenarios A-H plus the concurrency guard.

These tests encode the business rules the whole system turns on:

  A  a delivery is bounded by what the gate pass received
  B  received 23 / delivered 23 can never be delivered again
  C  one gate pass has many deliveries
  D  one delivery draws from many gate passes, each balanced independently
  E  a quantity correction changes the balance and is fully audited
  F  cancelling a delivery returns the quantity to the pool
  G  a hotel can never receive another hotel's linen
  H  a return makes quantity deliverable again
  --  two concurrent writes cannot both consume the same balance

Quantities are asserted against the *canonical engine* output, not against a
re-implementation of the arithmetic, so a router that drifts from the engine
fails here instead of silently disagreeing on screen.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import encrypt_dict
from bill_service.models import (
    DeliveryCancel,
    DeliveryCorrection,
    DeliveryCreate,
    DeliveryItem,
    DeliveryItemCorrection,
)
from bill_service.routers import deliveries, gatepasses
from bill_service.services import balance_engine as be

from conftest import auth_user

GP_SENSITIVE = ["client_name", "items", "notes"]
DL_SENSITIVE = ["client_name", "items", "notes"]


def _now():
    return datetime.now(timezone.utc)


def _gp_item(name, qty, spec=None):
    return {
        "item_name": name,
        "category": "Bed",
        "specification": spec,
        "client_qty": qty,
        "received_qty": qty,
        "difference": 0,
        "mismatch_reason": None,
        "mismatch_notes": None,
    }


async def seed_gate_pass(mocked_db, *, items, client="Sunshine Hotel", status="RECEIVED"):
    doc = {
        "gate_pass_number": f"GP-{ObjectId()}",
        "client_name": client,
        "receiving_date": _now(),
        "received_by": "System",
        "items": items,
        "status": status,
        "notes": "",
        "quotation_id": None,
        "adjustments": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    res = await mocked_db["gatepasses_collection"].insert_one(encrypt_dict(doc, GP_SENSITIVE))
    return str(res.inserted_id)


def _payload(gp_id, items, *, client="Sunshine Hotel", notes=None):
    return DeliveryCreate(
        client_name=client,
        delivered_by="Rider",
        received_by="Client",
        delivery_date=_now(),
        items=[
            DeliveryItem(item_name=n, specification=s, quantity=q, gate_pass_id=gp_id)
            for n, q, s in items
        ],
        notes=notes,
    )


async def _list_deliveries(**overrides):
    """Call the list route with its FastAPI defaults made explicit.

    Invoked directly, the `Query(...)` defaults arrive as sentinel objects, so
    every optional filter has to be passed as None rather than omitted.
    """
    params = {
        "client_name": None,
        "gate_pass_id": None,
        "date_from": None,
        "date_to": None,
        "include_cancelled": False,
    }
    params.update(overrides)
    return await deliveries.list_deliveries(current_user=auth_user(), **params)


def _request():
    return type(
        "Req", (), {"headers": {}, "method": "POST", "url": type("U", (), {"path": "/deliveries"})()}
    )()


def _create(payload, *, user=None):
    return deliveries.create_delivery(
        payload,
        current_user=user or auth_user(),
        request=_request(),
    )


def _error_detail(exc: HTTPException):
    detail = exc.value.detail
    if isinstance(detail, dict):
        return detail
    return {"code": "ERROR", "message": str(detail)}


async def _balance(mocked_db, gp_id):
    """Canonical remaining balance for one pass, straight from the engine."""
    from bill_service.services import operations_context as ctx

    gps = await ctx.load_gate_passes([gp_id])
    deliveries_, returns = await ctx.load_movements([gp_id])
    return ctx.balances_for(gps, ctx.delivered_by_gate_pass(deliveries_), ctx.returned_by_gate_pass(returns))[
        gp_id
    ]


# ── Scenario A: the delivery is bounded by what was received ──────────────────
async def test_scenario_a_delivery_cannot_exceed_received(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 20)])

    with pytest.raises(HTTPException) as exc:
        await _create(_payload(gp_id, [("Pillow", 21, None)]))

    assert exc.value.status_code == 400
    assert _error_detail(exc)["code"] == "DELIVERY_NOT_ALLOWED"
    assert await mocked_db["deliveries_collection"].count_documents({}) == 0

    # The exact received quantity is allowed.
    created = await _create(_payload(gp_id, [("Pillow", 20, None)]))
    assert created["items"][0]["quantity"] == 20


# ── Scenario B: a fully delivered item is not deliverable twice ───────────────
async def test_scenario_b_received_23_delivered_23_never_again(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 23)])

    await _create(_payload(gp_id, [("Pillow", 23, None)]))

    with pytest.raises(HTTPException) as exc:
        await _create(_payload(gp_id, [("Pillow", 23, None)]))

    detail = _error_detail(exc)
    assert exc.value.status_code == 400
    assert "already fully delivered" in detail["errors"][0]["detail"]

    # Balance is unchanged by the rejected attempt.
    balance = await _balance(mocked_db, gp_id)
    assert balance["totals"]["received_qty"] == 23
    assert balance["totals"]["delivered_qty"] == 23
    assert balance["totals"]["outstanding_delivery_qty"] == 0


async def test_scenario_b_partial_then_remainder_is_allowed(mocked_db):
    """23 received, 10 then 13 delivered, is two valid deliveries, not a bug."""
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 23)])

    await _create(_payload(gp_id, [("Pillow", 10, None)]))
    await _create(_payload(gp_id, [("Pillow", 13, None)]))

    balance = await _balance(mocked_db, gp_id)
    assert balance["totals"]["delivered_qty"] == 23
    assert balance["totals"]["outstanding_delivery_qty"] == 0
    assert await mocked_db["deliveries_collection"].count_documents({}) == 2


# ── Scenario C: one gate pass, many deliveries ────────────────────────────────
async def test_scenario_c_one_gate_pass_many_deliveries(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10), _gp_item("Towel", 5)])

    d1 = await _create(_payload(gp_id, [("Pillow", 4, None)]))
    d2 = await _create(_payload(gp_id, [("Pillow", 3, None), ("Towel", 5, None)]))

    assert d1["id"] != d2["id"]

    # Every delivery is listed against the pass it came from.
    listed = await gatepasses.list_gate_pass_deliveries(gp_id, auth_user())
    assert {d["id"] for d in listed["deliveries"]} == {d1["id"], d2["id"]}

    balance = await _balance(mocked_db, gp_id)
    assert balance["totals"]["delivered_qty"] == 12
    assert balance["totals"]["outstanding_delivery_qty"] == 3


# ── Scenario D: one delivery, many gate passes, independent balances ──────────
async def test_scenario_d_multi_gate_pass_delivery_balances_each_pass(mocked_db):
    gp_a = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)], client="Sunshine Hotel")
    gp_b = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 8)], client="Sunshine Hotel")

    created = await deliveries.create_delivery(
        DeliveryCreate(
            client_name="Sunshine Hotel",
            delivered_by="Rider",
            received_by="Client",
            delivery_date=_now(),
            items=[
                DeliveryItem(item_name="Pillow", quantity=4, gate_pass_id=gp_a),
                DeliveryItem(item_name="Pillow", quantity=6, gate_pass_id=gp_b),
            ],
        ),
        current_user=auth_user(),
        request=_request(),
    )

    assert set(created["source_gate_pass_ids"]) == {gp_a, gp_b}

    bal_a = await _balance(mocked_db, gp_a)
    bal_b = await _balance(mocked_db, gp_b)
    # 4 out of A, 6 out of B — NOT 10 out of each.
    assert bal_a["totals"]["delivered_qty"] == 4
    assert bal_a["totals"]["outstanding_delivery_qty"] == 6
    assert bal_b["totals"]["delivered_qty"] == 6
    assert bal_b["totals"]["outstanding_delivery_qty"] == 2


async def test_scenario_d_oversell_on_secondary_pass_is_rejected(mocked_db):
    """A line attributed to pass B is bounded by B, even when A has stock."""
    gp_a = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 50)], client="Sunshine Hotel")
    gp_b = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 3)], client="Sunshine Hotel")

    with pytest.raises(HTTPException) as exc:
        await deliveries.create_delivery(
            DeliveryCreate(
                client_name="Sunshine Hotel",
                delivered_by="Rider",
                received_by="Client",
                delivery_date=_now(),
                items=[
                    DeliveryItem(item_name="Pillow", quantity=5, gate_pass_id=gp_a),
                    DeliveryItem(item_name="Pillow", quantity=4, gate_pass_id=gp_b),
                ],
            ),
            current_user=auth_user(),
            request=_request(),
        )

    errors = _error_detail(exc)["errors"]
    assert any(e["gate_pass_id"] == gp_b and e["available"] == 3 for e in errors)
    assert await mocked_db["deliveries_collection"].count_documents({}) == 0


# ── Scenario E: corrections are audited and move the balance ──────────────────
async def test_scenario_e_correction_records_reason_and_moves_balance(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 50)])
    created = await _create(_payload(gp_id, [("Pillow", 50, None)]), user=auth_user("Alice"))

    corrected = await deliveries.correct_delivery_items(
        created["id"],
        DeliveryCorrection(
            reason="Hotel confirmed 3 fewer pillows were returned",
            items=[
                DeliveryItemCorrection(
                    gate_pass_id=gp_id,
                    item_name="Pillow",
                    quantity=47,
                )
            ],
        ),
        current_user=auth_user("Bob"),
    )

    assert corrected["items"][0]["quantity"] == 47
    entry = corrected["corrections"][-1]
    assert entry["reason"] == "Hotel confirmed 3 fewer pillows were returned"
    assert entry["corrected_by"] == "Bob"
    assert entry["corrected_by_id"] == "u-Bob"
    change = entry["changes"][0]
    assert change["item_name"] == "Pillow"
    assert change["original_quantity"] == 50
    assert change["corrected_quantity"] == 47
    assert change["delta"] == -3
    assert change["gate_pass_id"] == gp_id

    balance = await _balance(mocked_db, gp_id)
    assert balance["totals"]["delivered_qty"] == 47
    assert balance["totals"]["outstanding_delivery_qty"] == 3

    # The immutable event ledger records the same thing.
    events = await mocked_db["linen_events_collection"].find({"entity_id": created["id"]}).to_list(20)
    types = [e["event_type"] for e in events]
    assert "DELIVERY_CORRECTED" in types


async def test_scenario_e_correction_cannot_exceed_balance(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    created = await _create(_payload(gp_id, [("Pillow", 6, None)]))

    with pytest.raises(HTTPException) as exc:
        await deliveries.correct_delivery_items(
            created["id"],
            DeliveryCorrection(
                reason="typo",
                items=[DeliveryItemCorrection(gate_pass_id=gp_id, item_name="Pillow", quantity=11)],
            ),
            current_user=auth_user(),
        )

    assert exc.value.status_code == 400
    detail = await deliveries.get_delivery(created["id"], auth_user())
    assert detail["items"][0]["quantity"] == 6  # unchanged


async def test_scenario_e_correction_requires_a_reason(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    created = await _create(_payload(gp_id, [("Pillow", 6, None)]))

    with pytest.raises(Exception):
        await deliveries.correct_delivery_items(
            created["id"],
            DeliveryCorrection(
                items=[DeliveryItemCorrection(gate_pass_id=gp_id, item_name="Pillow", quantity=5)]
            ),
            current_user=auth_user(),
        )


# ── Scenario F: cancelling a delivery returns the quantity ────────────────────
async def test_scenario_f_cancel_restores_the_balance(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    created = await _create(_payload(gp_id, [("Pillow", 10, None)]))

    cancelled = await deliveries.cancel_delivery(
        created["id"],
        DeliveryCancel(reason="Recorded against the wrong hotel pass"),
        current_user=auth_user("Bob"),
    )
    assert cancelled["status"] == "CANCELLED"

    balance = await _balance(mocked_db, gp_id)
    assert balance["totals"]["delivered_qty"] == 0
    assert balance["totals"]["outstanding_delivery_qty"] == 10

    # Cancelled stock is deliverable again.
    again = await _create(_payload(gp_id, [("Pillow", 10, None)]))
    assert again["items"][0]["quantity"] == 10


# ── Scenario G: hotels can never be mixed ─────────────────────────────────────
async def test_scenario_g_delivery_cannot_mix_hotels(mocked_db):
    gp_a = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)], client="Sunshine Hotel")
    gp_b = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)], client="Grand Plaza")

    with pytest.raises(HTTPException) as exc:
        await deliveries.create_delivery(
            DeliveryCreate(
                client_name="Sunshine Hotel",
                delivered_by="Rider",
                received_by="Client",
                delivery_date=_now(),
                items=[
                    DeliveryItem(item_name="Pillow", quantity=1, gate_pass_id=gp_a),
                    DeliveryItem(item_name="Pillow", quantity=1, gate_pass_id=gp_b),
                ],
            ),
            current_user=auth_user(),
            request=_request(),
        )

    assert exc.value.status_code == 400
    assert "cannot mix hotels" in str(exc.value.detail).lower()


async def test_scenario_g_client_name_spelling_is_normalised(mocked_db):
    """' sunshine hotel ' is the same tenant as 'Sunshine Hotel'."""
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 5)], client="Sunshine Hotel")

    created = await _create(_payload(gp_id, [("Pillow", 2, None)], client="  SUNSHINE hotel  "))
    assert created["items"][0]["quantity"] == 2


# ── Scenario H: a return makes the quantity deliverable again ─────────────────
async def test_scenario_h_return_reopens_the_balance(mocked_db):
    from bill_service.routers import returns as returns_router

    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    await _create(_payload(gp_id, [("Pillow", 10, None)]))

    balance = await _balance(mocked_db, gp_id)
    assert balance["totals"]["outstanding_delivery_qty"] == 0

    await mocked_db["returns_collection"].insert_one(
        encrypt_dict(
            {
                "gate_pass_id": gp_id,
                "client_name": "Sunshine Hotel",
                "return_date": _now(),
                    "items": [
                        {
                            "item_name": "Pillow",
                            "specification": None,
                            "returned_qty": 4,
                            "action": "RECEIVE_BACK",
                            "resend_status": "PENDING",
                        }
                    ],
                    "status": "RETURNED",
                "notes": "",
                "created_at": _now(),
            },
            DL_SENSITIVE,
        )
    )

    balance = await _balance(mocked_db, gp_id)
    assert balance["totals"]["returned_back_qty"] == 4
    assert balance["totals"]["outstanding_delivery_qty"] == 4

    again = await _create(_payload(gp_id, [("Pillow", 4, None)]))
    assert again["items"][0]["quantity"] == 4


# ── Concurrency guard: two racing writes cannot both consume the balance ─────
async def test_concurrent_writes_cannot_both_claim_the_same_balance(mocked_db, monkeypatch):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])

    # Simulate the race: a rival delivery lands after the pre-flight snapshot
    # but before the post-write re-check reads state.
    from bill_service.services import operations_context as ctx

    original = ctx.load_movements
    calls = {"n": 0}

    async def racing_load(ids=None):
        calls["n"] += 1
        result = await original(ids)
        if calls["n"] == 1:  # right after the pre-flight read
            await mocked_db["deliveries_collection"].insert_one(
                encrypt_dict(
                    {
                        "gate_pass_id": gp_id,
                        "source_gate_pass_ids": [gp_id],
                        "client_name": "Sunshine Hotel",
                        "delivery_date": _now(),
                        "items": [
                            {
                                "item_name": "Pillow",
                                "specification": None,
                                "gate_pass_id": gp_id,
                                "quantity": 10,
                            }
                        ],
                        "status": "DELIVERED",
                        "notes": "",
                        "created_at": _now(),
                    },
                    DL_SENSITIVE,
                )
            )
        return result

    monkeypatch.setattr(deliveries.ctx, "load_movements", racing_load)

    with pytest.raises(HTTPException) as exc:
        await _create(_payload(gp_id, [("Pillow", 10, None)]))

    assert exc.value.status_code == 409
    assert _error_detail(exc)["code"] == "DELIVERY_CONFLICT"

    # Our own insert was rolled back; only the rival delivery survives.
    remaining = await mocked_db["deliveries_collection"].count_documents({})
    assert remaining == 1


# ── Duplicate lines must be rejected, not silently merged ─────────────────────
async def test_duplicate_lines_for_one_pass_item_are_rejected(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])

    with pytest.raises(HTTPException) as exc:
        await deliveries.create_delivery(
            DeliveryCreate(
                client_name="Sunshine Hotel",
                delivered_by="Rider",
                received_by="Client",
                delivery_date=_now(),
                items=[
                    DeliveryItem(item_name="Pillow", quantity=3, gate_pass_id=gp_id),
                    DeliveryItem(item_name="Pillow", quantity=4, gate_pass_id=gp_id),
                ],
            ),
            current_user=auth_user(),
            request=_request(),
        )

    errors = _error_detail(exc)["errors"]
    assert any("listed more than once" in e["detail"] for e in errors)
    assert await mocked_db["deliveries_collection"].count_documents({}) == 0


# ── The availability endpoint agrees with what creation will allow ───────────
async def test_available_endpoint_matches_enforcement(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10), _gp_item("Towel", 4)])
    await _create(_payload(gp_id, [("Pillow", 6, None)]))

    available = await deliveries.available_quantities(
        client_name="Sunshine Hotel", current_user=auth_user()
    )

    rows = {r["item_name"]: r for gp in available["gate_passes"] for r in gp["items"]}
    assert rows["Pillow"]["received_qty"] == 10
    assert rows["Pillow"]["delivered_qty"] == 6
    assert rows["Pillow"]["available_qty"] == 4
    assert rows["Towel"]["available_qty"] == 4
    assert all(gp["client_name"] == "Sunshine Hotel" for gp in available["gate_passes"])
    assert available["total_available_qty"] == 8  # 4 Pillows + 4 Towels


# ── The engine's invariant predicate agrees with stored state ────────────────
async def test_find_oversell_violations_flags_a_corrupt_ledger(mocked_db):
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 5)])
    gps = [{"id": gp_id, "items": [_gp_item("Pillow", 5)], "status": "RECEIVED"}]

    assert be.find_oversell_violations(gps, {gp_id: {be.item_key("Pillow"): 5}}, {}) == []
    assert be.find_oversell_violations(gps, {gp_id: {be.item_key("Pillow"): 6}}, {}) != []


async def test_legacy_delivery_without_source_field_still_balances(mocked_db):
    """Deliveries written before item-level attribution must keep working."""
    from bill_service.services import operations_context as ctx

    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    await mocked_db["deliveries_collection"].insert_one(
        encrypt_dict(
            {
                "gate_pass_id": gp_id,
                "client_name": "Sunshine Hotel",
                "delivery_date": _now(),
                "items": [{"item_name": "Pillow", "specification": None, "quantity": 4}],
                "status": "DELIVERED",
                "notes": "",
                "created_at": _now(),
            },
            DL_SENSITIVE,
        )
    )

    gps = await ctx.load_gate_passes([gp_id])
    deliveries_, returns = await ctx.load_movements([gp_id])
    balances = ctx.balances_for(
        gps, ctx.delivered_by_gate_pass(deliveries_), ctx.returned_by_gate_pass(returns)
    )
    assert balances[gp_id]["totals"]["delivered_qty"] == 4
    assert balances[gp_id]["totals"]["outstanding_delivery_qty"] == 6


async def test_reconciliation_flags_a_spec_level_oversell(mocked_db):
    """A King-spec oversell must not be masked by an unrelated Queen-spec line."""
    from bill_service.services import operations_context as ctx

    gp_id = await seed_gate_pass(
        mocked_db,
        items=[_gp_item("Pillow", 3, spec="King"), _gp_item("Pillow", 40, spec="Queen")],
    )
    await mocked_db["deliveries_collection"].insert_one(
        encrypt_dict(
            {
                "gate_pass_id": gp_id,
                "source_gate_pass_ids": [gp_id],
                "client_name": "Sunshine Hotel",
                "delivery_date": _now(),
                "items": [
                    {
                        "item_name": "Pillow",
                        "specification": "King",
                        "gate_pass_id": gp_id,
                        "quantity": 5,  # only 3 King pillows were received
                    }
                ],
                "status": "DELIVERED",
                "notes": "",
                "created_at": _now(),
            },
            DL_SENSITIVE,
        )
    )

    gps = await ctx.load_gate_passes([gp_id])
    dels, _ = await ctx.load_movements([gp_id])
    balance = ctx.balances_for(gps, ctx.delivered_by_gate_pass(dels))[gp_id]

    issues = be.detect_reconciliation_issues(balance, "RECEIVED", False, {})
    oversold = [i for i in issues if i["code"] == "OVER_DELIVERED"]
    assert oversold, "the King-spec oversell was not reported"
    # The report must name the offending specification, not just the item.
    assert "King" in oversold[0]["detail"]

    violations = be.find_oversell_violations(gps, ctx.delivered_by_gate_pass(dels))
    assert len(violations) == 1
    assert violations[0]["specification"] == "King"


# ── Catch-up delivery: a real delivery record, derived status ────────────────
async def test_catch_up_creates_a_real_delivery_and_derives_status(mocked_db):
    from bill_service.models import CatchUpDeliveryItem, GatePassCatchUpDelivery
    from bill_service.routers import gatepasses as gp_router

    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 12)])

    result = await gp_router.catch_up_delivery(
        gp_id,
        GatePassCatchUpDelivery(
            note="Physical dispatch happened before the app was in use",
            items=[CatchUpDeliveryItem(item_name="Pillow", quantity=12)],
        ),
        current_user=auth_user("Alice"),
    )

    assert result["status"] == "DELIVERED"

    # A real delivery row exists, not just a note.
    assert await mocked_db["deliveries_collection"].count_documents({}) == 1
    balance = await _balance(mocked_db, gp_id)
    assert balance["totals"]["delivered_qty"] == 12
    assert balance["totals"]["outstanding_delivery_qty"] == 0


async def test_catch_up_cannot_exceed_the_remaining_balance(mocked_db):
    from bill_service.models import CatchUpDeliveryItem, GatePassCatchUpDelivery
    from bill_service.routers import gatepasses as gp_router

    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    await _create(_payload(gp_id, [("Pillow", 7, None)]))

    with pytest.raises(HTTPException) as exc:
        await gp_router.catch_up_delivery(
            gp_id,
            GatePassCatchUpDelivery(
                note="trying to close the pass with the wrong count",
                items=[CatchUpDeliveryItem(item_name="Pillow", quantity=7)],
            ),
            current_user=auth_user(),
        )

    assert exc.value.status_code == 400
    assert await mocked_db["deliveries_collection"].count_documents({}) == 1  # only the valid one


async def test_catch_up_on_a_closed_pass_is_refused(mocked_db):
    from bill_service.models import CatchUpDeliveryItem, GatePassCatchUpDelivery
    from bill_service.routers import gatepasses as gp_router

    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 5)], status="DELIVERED")

    with pytest.raises(HTTPException) as exc:
        await gp_router.catch_up_delivery(
            gp_id,
            GatePassCatchUpDelivery(
                note="late entry",
                items=[CatchUpDeliveryItem(item_name="Pillow", quantity=1)],
            ),
            current_user=auth_user(),
        )
    assert exc.value.status_code == 409


async def test_delivery_list_carries_every_source_pass_balance(mocked_db):
    """The list screen must not have to re-derive progress from one pass.

    The table used to render a bar computed as `delivered / received` against
    the delivery's own gate_pass_id, which is meaningless for a delivery that
    drew from two passes: it credited all the pieces to one of them and showed
    the other as untouched. The list now reports each origin pass separately.
    """
    gp_a = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    gp_b = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 8)])

    created = await _create(
        DeliveryCreate(
            client_name="Sunshine Hotel",
            delivered_by="Rider",
            received_by="Client",
            delivery_date=_now(),
            items=[
                DeliveryItem(item_name="Pillow", quantity=4, gate_pass_id=gp_a),
                DeliveryItem(item_name="Pillow", quantity=6, gate_pass_id=gp_b),
            ],
        )
    )

    rows = await _list_deliveries()
    row = next(r for r in rows if r["id"] == created["id"])

    summaries = {s["gate_pass_id"]: s for s in row["source_gate_passes"]}
    assert set(summaries) == {gp_a, gp_b}

    assert summaries[gp_a]["totals"]["delivered_qty"] == 4
    assert summaries[gp_a]["totals"]["received_qty"] == 10
    assert summaries[gp_a]["totals"]["outstanding_delivery_qty"] == 6
    assert summaries[gp_b]["totals"]["delivered_qty"] == 6
    assert summaries[gp_b]["totals"]["outstanding_delivery_qty"] == 2
    # The client's hotel is echoed per pass so the row can be filtered by scope.
    assert {s["client_name"] for s in summaries.values()} == {"Sunshine Hotel"}


async def test_delivery_list_source_summaries_track_returns(mocked_db):
    """A pending return must raise the reported outstanding quantity."""
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 5)])
    await _create(
        DeliveryCreate(
            client_name="Sunshine Hotel",
            delivered_by="Rider",
            received_by="Client",
            delivery_date=_now(),
            items=[DeliveryItem(item_name="Pillow", quantity=5, gate_pass_id=gp_id)],
        )
    )
    await mocked_db["returns_collection"].insert_one(
        encrypt_dict(
            {
                "gate_pass_id": gp_id,
                "client_name": "Sunshine Hotel",
                "return_date": _now(),
                "items": [
                    {
                        "item_name": "Pillow",
                        "specification": None,
                        "returned_qty": 2,
                        "action": "RECEIVE_BACK",
                        "resend_status": "PENDING",
                    }
                ],
                "status": "RETURNED",
                "notes": "",
                "created_at": _now(),
            },
            DL_SENSITIVE,
        )
    )

    rows = await _list_deliveries()
    summary = rows[0]["source_gate_passes"][0]
    assert summary["totals"]["returned_back_qty"] == 2
    assert summary["totals"]["outstanding_delivery_qty"] == 2


async def test_gatepass_deliveries_slices_only_its_own_lines(mocked_db):
    """A pass's delivery history shows the lines attributed to that pass."""
    from bill_service.routers import gatepasses as gp_router

    gp_a = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    gp_b = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 8)])

    await _create(
        DeliveryCreate(
            client_name="Sunshine Hotel",
            delivered_by="Rider",
            received_by="Client",
            delivery_date=_now(),
            items=[
                DeliveryItem(item_name="Pillow", quantity=4, gate_pass_id=gp_a),
                DeliveryItem(item_name="Pillow", quantity=6, gate_pass_id=gp_b),
            ],
        )
    )

    hist = await gp_router.list_gate_pass_deliveries(gp_a, current_user=auth_user())
    assert hist["gate_pass_id"] == gp_a
    assert len(hist["deliveries"]) == 1
    lines = hist["deliveries"][0]["lines_from_this_gate_pass"]
    assert [(li["item_name"], li["quantity"]) for li in lines] == [("Pillow", 4)]


async def test_notification_pending_uses_canonical_balance(mocked_db):
    """The bell icon and the delivery form read the same numbers."""
    from bill_service.routers import notifications

    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10), _gp_item("Towel", 4)])
    await _create(
        DeliveryCreate(
            client_name="Sunshine Hotel",
            delivered_by="Rider",
            received_by="Client",
            delivery_date=_now(),
            items=[DeliveryItem(item_name="Pillow", quantity=6, gate_pass_id=gp_id)],
        )
    )

    entries = await notifications.gatepass_pending(client_name=None, current_user=auth_user())
    by_item = {e["item_name"]: e for e in entries}
    assert set(by_item) == {"Pillow", "Towel"}
    assert by_item["Pillow"]["pending"] == 4
    assert by_item["Pillow"]["delivered"] == 6
    assert by_item["Pillow"]["received"] == 10
    assert by_item["Towel"]["pending"] == 4
    assert by_item["Towel"]["delivered"] == 0

    summary = await notifications.notification_summary(client_name=None, current_user=auth_user())
    assert summary["pending_pieces"] == 8
    assert summary["pending_items"] == 2
    assert summary["pending_gate_passes"] == 1


async def test_linen_flow_attributes_each_pass_its_own_lines(mocked_db):
    """The hotel flow screen must not credit a shared delivery to one pass.

    It used to match deliveries with `d.gate_pass_id === gp.id`, so a delivery
    drawing from two passes put every piece on the primary pass and showed the
    second pass as fully outstanding.
    """
    from bill_service.routers import dashboard

    gp_a = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)])
    gp_b = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 8)])

    await _create(
        DeliveryCreate(
            client_name="Sunshine Hotel",
            delivered_by="Rider",
            received_by="Client",
            delivery_date=_now(),
            items=[
                DeliveryItem(item_name="Pillow", quantity=4, gate_pass_id=gp_a),
                DeliveryItem(item_name="Pillow", quantity=6, gate_pass_id=gp_b),
            ],
        )
    )

    flow = await dashboard.get_linen_flow(period="all")
    assert flow["totals"]["received_qty"] == 18
    assert flow["totals"]["delivered_qty"] == 10
    assert flow["totals"]["outstanding_delivery_qty"] == 8

    assert len(flow["hotels"]) == 1
    hotel = flow["hotels"][0]
    by_pass = {g["gate_pass_id"]: g for g in hotel["gate_passes"]}
    assert by_pass[gp_a]["totals"]["outstanding_delivery_qty"] == 6
    assert by_pass[gp_b]["totals"]["outstanding_delivery_qty"] == 2
    # The outstanding chip lists the line that is actually short, per pass.
    assert [i["outstanding_delivery_qty"] for i in by_pass[gp_a]["outstanding_items"]] == [6]
    assert [i["outstanding_delivery_qty"] for i in by_pass[gp_b]["outstanding_items"]] == [2]


async def test_linen_flow_outstanding_excludes_delivered_lines(mocked_db):
    """A pass fully delivered must report no outstanding lines, even if received."""
    from bill_service.routers import dashboard

    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 5), _gp_item("Towel", 2)])
    await _create(_payload(gp_id, [("Pillow", 5, None), ("Towel", 2, None)]))

    flow = await dashboard.get_linen_flow(period="all")
    gp_row = next(g for g in flow["hotels"][0]["gate_passes"] if g["gate_pass_id"] == gp_id)
    assert gp_row["totals"]["outstanding_delivery_qty"] == 0
    # Previously the "pending" chip listed every line with received_qty > 0,
    # so a fully delivered pass still advertised a shortfall.
    assert gp_row["outstanding_items"] == []


async def test_linen_flow_unlinked_deliveries_are_reported(mocked_db):
    """A delivery with no gate pass in the window is surfaced, not dropped."""
    from bill_service.routers import dashboard

    res = await mocked_db["deliveries_collection"].insert_one(
        encrypt_dict(
            {
                "delivery_date": _now(),
                "client_name": "Seabreeze Resort",
                "delivered_by": "Rider",
                "received_by": "Client",
                "items": [{"item_name": "Pillow", "specification": None, "quantity": 3}],
                "source_gate_pass_ids": [str(ObjectId())],
                "status": "DELIVERED",
                "notes": "",
                "created_at": _now(),
            },
            DL_SENSITIVE,
        )
    )
    assert res.inserted_id

    flow = await dashboard.get_linen_flow(period="all")
    hotel = next(h for h in flow["hotels"] if h["client_name"] == "Seabreeze Resort")
    assert len(hotel["unlinked_deliveries"]) == 1
    assert hotel["unlinked_deliveries"][0]["pieces"] == 3
    # Unlinked pieces are not folded into the pass balances, so they cannot
    # inflate a gate pass's delivered figure.
    assert hotel["totals"]["delivered_qty"] == 0


async def test_correction_is_rejected_when_the_delivery_moved_underneath_it(mocked_db, monkeypatch):
    """Two operators correcting one delivery must not silently clobber each other.

    The write used to be an unconditional `replace_one`, so whoever saved second
    overwrote the first correction *and* its audit record. The write is now
    conditional on the state that was read, and the loser gets a 409.
    """
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 50)])
    created = await _create(_payload(gp_id, [("Pillow", 20, None)]))

    # Bob's correction lands while Alice's request is mid-flight: after Alice's
    # endpoint has read the document, but before it writes.
    real_load_movements = deliveries.ctx.load_movements
    fired = {"done": False}

    async def _racing_load(gate_pass_ids=None):
        if not fired["done"]:
            fired["done"] = True
            await mocked_db["deliveries_collection"].update_one(
                {"_id": ObjectId(created["id"])},
                {"$set": {"updated_at": _now()}},
            )
        return await real_load_movements(gate_pass_ids)

    monkeypatch.setattr(deliveries.ctx, "load_movements", _racing_load)

    with pytest.raises(HTTPException) as exc:
        await deliveries.correct_delivery_items(
            created["id"],
            DeliveryCorrection(
                reason="Alice: recount",
                items=[DeliveryItemCorrection(gate_pass_id=gp_id, item_name="Pillow", quantity=15)],
            ),
            current_user=auth_user("Alice"),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "DELIVERY_CONFLICT"

    monkeypatch.undo()

    # Alice's correction never landed, and no audit entry was left behind.
    after = await deliveries.get_delivery(created["id"], current_user=auth_user())
    assert after["items"][0]["quantity"] == 20
    assert not (after.get("corrections") or [])


async def test_correction_post_write_guard_rolls_back(mocked_db, monkeypatch):
    """If the stored ledger is oversold after a correction, it is undone.

    Pre-flight validation reads a snapshot, so a delivery landing in between can
    invalidate it. The post-write re-check is the backstop: when it trips, the
    original document is written back and the operator is told to reload.
    """
    gp_id = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 50)])
    created = await _create(_payload(gp_id, [("Pillow", 20, None)]))

    original = await mocked_db["deliveries_collection"].find_one(
        {"_id": ObjectId(created["id"])}
    )

    # Stand in for another delivery consuming the balance between the snapshot
    # and the write. Only the post-write re-check consults this predicate.
    def _fake_violations(gps, delivered, returned):
        return [{"gate_pass_id": gp_id, "item_key": "Pillow", "delivered": 51, "received": 50}]

    monkeypatch.setattr(deliveries.be, "find_oversell_violations", _fake_violations)

    with pytest.raises(HTTPException) as exc:
        await deliveries.correct_delivery_items(
            created["id"],
            DeliveryCorrection(
                reason="racing recount",
                items=[DeliveryItemCorrection(gate_pass_id=gp_id, item_name="Pillow", quantity=19)],
            ),
            current_user=auth_user("Alice"),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "DELIVERY_CONFLICT"

    # The original document is back, byte for byte — no half-applied correction
    # and no audit entry for a change that was undone.
    restored = await mocked_db["deliveries_collection"].find_one(
        {"_id": ObjectId(created["id"])}
    )
    assert restored["items"] == original["items"]
    assert not (restored.get("corrections") or [])
    assert restored["updated_at"] == original["updated_at"]


async def test_bill_refuses_a_multi_pass_delivery_that_crosses_hotels(mocked_db):
    """A delivery spanning two hotels' passes cannot be billed as one tenant.

    The delivery endpoint rejects a mixed-hotel delivery up front, so this is
    defence in depth for records that predate that check: the bill validated
    only the gate pass named in the payload, which meant a cross-hotel delivery
    already on file could be billed under one hotel's contract.
    """
    from bill_service.routers import bills as bills_router
    from bill_service.models import BillCreate, BillItemIn

    gp_hotel_a = await seed_gate_pass(mocked_db, items=[_gp_item("Pillow", 10)], client="Sunshine Hotel")
    gp_hotel_b = await seed_gate_pass(mocked_db, items=[_gp_item("Towel", 10)], client="Seabreeze Resort")

    # Written straight to the collection, the way a pre-check record would be.
    legacy = await mocked_db["deliveries_collection"].insert_one(
        encrypt_dict(
            {
                "delivery_date": _now(),
                "client_name": "Sunshine Hotel",
                "delivered_by": "Rider",
                "received_by": "Client",
                "items": [
                    {"item_name": "Pillow", "specification": None, "quantity": 5,
                     "gate_pass_id": gp_hotel_a},
                    {"item_name": "Towel", "specification": None, "quantity": 5,
                     "gate_pass_id": gp_hotel_b},
                ],
                "gate_pass_id": gp_hotel_a,
                "source_gate_pass_ids": [gp_hotel_a, gp_hotel_b],
                "status": "DELIVERED",
                "notes": "",
                "created_at": _now(),
            },
            DL_SENSITIVE,
        )
    )
    legacy_id = str(legacy.inserted_id)

    with pytest.raises(HTTPException) as exc:
        await bills_router.create_bill(
            BillCreate(
                client_name="Sunshine Hotel",
                delivery_ids=[legacy_id],
                items=[BillItemIn(item_name="Pillow", quantity=5, unit_price=10)],
                notes="",
            ),
            current_user=auth_user(),
            request=type("R", (), {"headers": {"Idempotency-Key": "x1"}})(),
        )
    assert exc.value.status_code == 400
    assert "Hotel mismatch" in str(exc.value.detail)

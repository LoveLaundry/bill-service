"""Phase 5: the monthly grid verified against the REST of the application.

``test_monthly.py`` proves each monthly endpoint behaves correctly on its own.
These tests walk the sequence the UI actually drives - enter, confirm,
activate - across all three kinds, and then check the result against the OTHER
screens' sources of truth (the pending gate-pass list and the balance engine).
That is the property that actually matters: a month entered in the grid has to
show up in receiving, dispatch and stock exactly once, and only from the moment
it is activated.

The last test pins the response shape the TypeScript types in
``quotations-ui/src/types/monthly.ts`` are written against. Those types are
hand-written, so this is the guard against the two repos drifting apart.
"""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from bill_service.crypto_helper import decrypt_dict
from bill_service.models import (
    BillCreate,
    BillItemIn,
    MonthlyDayConfirm,
    MonthlyMatrixResponse,
    MonthlyQuantitiesUpdate,
)
from bill_service.routers import bills as bills_router
from bill_service.routers import monthly as m
from bill_service.routers import shop_bills as shop_bills_router
from bill_service.routers.deliveries import activate_delivery, pending_gatepasses
from bill_service.routers.gatepasses import update_gate_pass_status
from bill_service.services import manual_references
from bill_service.services import operations_context as ctx

GP_SENSITIVE = ["client_name", "items", "notes"]

CLIENT = "Hotel Amagi"
Y = 2026
MO = 9


def _req():
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/monthly/receiving/Hotel/2026/9/1/confirm",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )


def user(name="Alice"):
    return {"auth_id": f"u-{name}", "user_name": name, "role": "ADMIN"}


async def put(kind, quantities, day=1, client=CLIENT, year=Y, month=MO):
    return await m.update_day_quantities(
        kind, client, year, month, day, MonthlyQuantitiesUpdate(quantities=quantities),
        current_user=user(),
    )


async def confirm(kind, day=1, body=None, client=CLIENT, year=Y, month=MO):
    return await m.confirm_monthly_day(
        kind, client, year, month, day, body or MonthlyDayConfirm(),
        current_user=user(), request=_req(),
    )


async def gate_passes(mocked_db):
    out = []
    for doc in await mocked_db["gatepasses_collection"].find({}).to_list(None):
        d = decrypt_dict(doc, GP_SENSITIVE)
        d["id"] = str(d["_id"])
        out.append(d)
    return out


async def deliveries(mocked_db):
    out = []
    for doc in await mocked_db["deliveries_collection"].find({}).to_list(None):
        d = decrypt_dict(doc, ["client_name", "items", "notes"])
        d["id"] = str(d["_id"])
        out.append(d)
    return out


async def pending_rows():
    return await pending_gatepasses(client_name=CLIENT, current_user=user())


async def matrix(kind, client=CLIENT, year=Y, month=MO):
    """The matrix exactly as the browser receives it (response model applied)."""
    raw = await m.get_monthly_matrix(kind, client, year, month, quotation_id=None, current_user=user())
    return MonthlyMatrixResponse(**raw).model_dump()


async def balance_for(gp_id):
    gps = await ctx.load_gate_passes([gp_id])
    moved, returns = await ctx.load_movements([gp_id])
    return ctx.balances_for(gps, ctx.delivered_by_gate_pass(moved), ctx.returned_by_gate_pass(returns))[gp_id]


# ---------------------------------------------------------------------------
# DRAFT invisibility / activation, seen from the other screens
# ---------------------------------------------------------------------------
async def test_monthly_manual_references_round_trip_to_gate_pass(mocked_db):
    payload = MonthlyQuantitiesUpdate(
        quantities={"Towel||": 4},
        bill_number="DAY-BILL-17",
        gate_pass_number="HOTEL-GP-17",
        alrs_number="ALRS-17",
    )
    await m.update_day_quantities(
        "receiving", CLIENT, Y, MO, 1, payload, current_user=user()
    )
    day = next(item for item in (await matrix("receiving"))["days"] if item["day"] == 1)
    assert day["bill_number"] == "DAY-BILL-17"
    assert day["gate_pass_number"] == "HOTEL-GP-17"
    assert day["alrs_number"] == "ALRS-17"

    await confirm("receiving")
    gate_pass = (await gate_passes(mocked_db))[0]
    assert gate_pass["gate_pass_number"] == "HOTEL-GP-17"
    assert gate_pass["manual_gate_pass_number"] == "HOTEL-GP-17"
    assert gate_pass["manual_bill_number"] == "DAY-BILL-17"
    assert gate_pass["alrs_number"] == "ALRS-17"
    bill = await bills_router.create_bill(
        BillCreate(
            client_name=CLIENT,
            gate_pass_id=gate_pass["id"],
            items=[BillItemIn(item_name="Towel", unit_price=5, quantity=4)],
        ),
        user(),
        request=_req(),
    )
    assert bill["receiving_date"] == gate_pass["receiving_date"]
    assert bill["manual_bill_number"] == "DAY-BILL-17"
    assert bill["manual_gate_pass_number"] == "HOTEL-GP-17"
    assert bill["alrs_number"] == "ALRS-17"


async def test_manual_references_conflict_case_and_whitespace_insensitively():
    first = MonthlyQuantitiesUpdate(
        quantities={"Towel||": 1}, bill_number="Hotel   Bill 42"
    )
    await m.update_day_quantities(
        "receiving", CLIENT, Y, MO, 1, first, current_user=user()
    )

    duplicate = MonthlyQuantitiesUpdate(
        quantities={}, gate_pass_number=" hotel bill 42 "
    )
    with pytest.raises(HTTPException) as exc:
        await m.update_day_quantities(
            "receiving", CLIENT, Y, MO, 2, duplicate, current_user=user()
        )
    assert exc.value.status_code == 409


async def test_generated_bill_reference_skips_manually_reserved_number(mocked_db):
    await mocked_db["manual_references_collection"].create_index(
        "normalized", unique=True
    )
    await manual_references.reserve_references(
        "monthly:manual-number-test",
        {"alrs_number": "SB-ABCDEFGH"},
    )
    assert not await manual_references.reserve_generated_bill_number("sb-abcdefgh")


async def test_shop_bill_generator_avoids_reserved_monthly_references(
    mocked_db, monkeypatch
):
    await mocked_db["manual_references_collection"].create_index(
        "normalized", unique=True
    )
    await manual_references.reserve_references(
        "monthly:manual-number-test",
        {"bill_number": "SB-22222222"},
    )
    candidates = iter([list("2" * 8), list("3" * 8)])
    monkeypatch.setattr(
        shop_bills_router.random, "choices", lambda _chars, k: next(candidates)
    )

    assert await shop_bills_router._generate_bill_number() == "SB-33333333"


async def test_receiving_draft_hidden_until_activated(mocked_db):
    """An unactivated monthly gate pass is not stock anyone may deliver."""
    await put("receiving", {"Duvet Cover||": 50})
    await confirm("receiving")

    gp = (await gate_passes(mocked_db))[0]
    assert gp["status"] == "DRAFT"
    assert await pending_rows() == []

    await update_gate_pass_status(gp["id"], "RECEIVED", current_user=user())

    rows = await pending_rows()
    assert len(rows) == 1
    assert rows[0]["total_pending"] == 50
    assert rows[0]["items"][0]["pending_qty"] == 50


async def test_delivery_draft_moves_no_stock_until_activated(mocked_db):
    """Confirming a delivery day must not move the balance; activation must."""
    await put("receiving", {"Duvet Cover||": 50})
    await confirm("receiving")
    gp = (await gate_passes(mocked_db))[0]
    await update_gate_pass_status(gp["id"], "RECEIVED", current_user=user())

    await put("delivery", {"Duvet Cover||": 10})
    await confirm("delivery", body=MonthlyDayConfirm(source_mode="same_day"))
    dlv = (await deliveries(mocked_db))[0]
    assert dlv["status"] == "DRAFT"

    # Still 50 available: the DRAFT delivery is recorded, not applied.
    assert (await pending_rows())[0]["total_pending"] == 50
    live_summary = (await matrix("receiving"))["operations_summary"]
    assert live_summary["totals"]["received_qty"]["pcs"] == 50
    assert live_summary["totals"]["delivered_qty"]["pcs"] == 0
    assert live_summary["totals"]["outstanding_delivery_qty"]["pcs"] == 50
    before = await balance_for(gp["id"])

    await activate_delivery(dlv["id"], current_user=user())

    after = await balance_for(gp["id"])
    assert after["totals"]["outstanding_delivery_qty"] == before["totals"]["outstanding_delivery_qty"] - 10
    assert (await pending_rows())[0]["total_pending"] == 40
    assert (await gate_passes(mocked_db))[0]["status"] == "PARTIALLY_DELIVERED"
    live_summary = (await matrix("receiving"))["operations_summary"]
    assert live_summary["totals"]["delivered_qty"]["pcs"] == 10
    assert live_summary["totals"]["outstanding_delivery_qty"]["pcs"] == 40


async def test_rewash_recorded_and_counted_without_activation(mocked_db):
    """Rewash is a receiving-side record: confirmed means it already counts."""
    await put("rewash", {"Duvet Cover||": 4})
    result = await confirm("rewash", body=MonthlyDayConfirm(chargeable=False))

    assert len(result["days"]["1"]["rewash_ids"]) == 1
    rewash = decrypt_dict(
        await mocked_db["rewashes_collection"].find_one({}), ["client_name", "notes"]
    )
    assert rewash["status"] == "RECORDED"
    assert rewash["chargeable"] is False
    # It creates no gate pass and no delivery.
    assert await gate_passes(mocked_db) == []
    assert await deliveries(mocked_db) == []


# ---------------------------------------------------------------------------
# A whole month, all three kinds
# ---------------------------------------------------------------------------
async def test_month_walked_end_to_end(mocked_db):
    """Day 1 receiving -> delivery, day 2 rewash, then read every grid back."""
    # --- day 1: receiving, activated -------------------------------------
    await put("receiving", {"Duvet Cover||": 50, "Towel||": 20}, day=1)
    await confirm("receiving", day=1)
    gp = (await gate_passes(mocked_db))[0]
    await update_gate_pass_status(gp["id"], "RECEIVED", current_user=user())

    # --- day 1: deliveries, activated ------------------------------------
    await put("delivery", {"Duvet Cover||": 30, "Towel||": 10}, day=1)
    await confirm("delivery", day=1, body=MonthlyDayConfirm(source_mode="same_day"))
    dlv = (await deliveries(mocked_db))[0]
    await activate_delivery(dlv["id"], current_user=user())

    # --- day 2: rewash, recorded directly --------------------------------
    await put("rewash", {"Towel||": 6}, day=2)
    await confirm("rewash", day=2)

    # Stock after the month: 20 duvet covers and 10 towels still owed.
    rows = await pending_rows()
    assert len(rows) == 1
    pending_by_item = {i["item_name"]: i["pending_qty"] for i in rows[0]["items"]}
    assert pending_by_item == {"Duvet Cover": 20, "Towel": 10}

    # Every grid reports only its own days, and day 1 of each is CONFIRMED.
    recv = await matrix("receiving")
    recv_by_day = {d["day"]: d for d in recv["days"]}
    assert recv_by_day[1]["status"] == "CONFIRMED"
    assert recv_by_day[1]["gate_pass_ids"] == [gp["id"]]
    assert recv_by_day[2]["status"] == "EMPTY"
    assert recv["cells"]["Duvet Cover||"]["1"] == 50

    dlv_matrix = await matrix("delivery")
    dlv_by_day = {d["day"]: d for d in dlv_matrix["days"]}
    assert dlv_by_day[1]["status"] == "CONFIRMED"
    assert dlv_by_day[1]["delivery_ids"] == [dlv["id"]]
    assert dlv_matrix["cells"]["Duvet Cover||"]["1"] == 30

    rw_matrix = await matrix("rewash")
    rw_by_day = {d["day"]: d for d in rw_matrix["days"]}
    assert rw_by_day[2]["status"] == "CONFIRMED"
    assert len(rw_by_day[2]["rewash_ids"]) == 1
    assert rw_by_day[1]["status"] == "EMPTY"
    assert rw_matrix["cells"]["Towel||"]["2"] == 6


async def test_auto_mode_drains_oldest_gate_pass_first(mocked_db):
    """`auto` is FIFO, and splits across passes as one delivery each."""
    for day in (1, 2):
        await put("receiving", {"Duvet Cover||": 50}, day=day)
        await confirm("receiving", day=day)
    first, second = await gate_passes(mocked_db)
    for gp in (first, second):
        await update_gate_pass_status(gp["id"], "RECEIVED", current_user=user())

    # Day 3 delivers 60: the whole of the oldest pass plus 10 from the next.
    await put("delivery", {"Duvet Cover||": 60}, day=3)
    await confirm("delivery", day=3, body=MonthlyDayConfirm(source_mode="auto"))

    # One DRAFT delivery per source pass, quantities split FIFO.
    drafts = {d["gate_pass_id"]: d for d in await deliveries(mocked_db)}
    assert set(drafts) == {first["id"], second["id"]}
    assert drafts[first["id"]]["items"][0]["quantity"] == 50
    assert drafts[second["id"]]["items"][0]["quantity"] == 10
    assert all(d["status"] == "DRAFT" for d in drafts.values())

    # Both must be activated before either pass reports as delivered.
    for gp_id in (first["id"], second["id"]):
        await activate_delivery(drafts[gp_id]["id"], current_user=user())

    statuses = {g["id"]: g["status"] for g in await gate_passes(mocked_db)}
    assert statuses[first["id"]] == "DELIVERED"
    assert statuses[second["id"]] == "PARTIALLY_DELIVERED"

    rows = await pending_rows()
    assert len(rows) == 1
    assert rows[0]["gate_pass_id"] == second["id"]
    assert rows[0]["total_pending"] == 40


async def test_quotation_id_travels_onto_created_records(mocked_db):
    """The page-level quotation picker stamps every record the day creates."""
    await put("receiving", {"Duvet Cover||": 50})
    await confirm("receiving", body=MonthlyDayConfirm(quotation_id="65f0aa0000000000000000aa"))

    gp = (await gate_passes(mocked_db))[0]
    assert gp["quotation_id"] == "65f0aa0000000000000000aa"
    assert gp["origin"]["kind"] == "monthly"
    assert gp["origin"]["quotation_id"] == "65f0aa0000000000000000aa"

    matrix = await m.get_monthly_matrix("receiving", CLIENT, Y, MO, quotation_id=None, current_user=user())
    assert matrix["quotation_id"] == "65f0aa0000000000000000aa"


# ---------------------------------------------------------------------------
# Contract with the frontend
# ---------------------------------------------------------------------------
async def test_matrix_shape_matches_frontend_types():
    """Pin the keys ``quotations-ui/src/types/monthly.ts`` declares.

    A missing key here is a silent `undefined` in the browser, not an error, so
    it has to fail here instead.
    """
    await put("receiving", {"Duvet Cover||": 50}, day=1)
    await confirm("receiving")
    raw = await m.get_monthly_matrix("receiving", CLIENT, Y, MO, quotation_id=None, current_user=user())

    # The route declares response_model=MonthlyMatrixResponse, so FastAPI
    # filters and defaults the payload on the way out. Replaying that here is
    # what the browser actually receives - a field the model drops would be a
    # silent `undefined` in the page.
    data = MonthlyMatrixResponse(**raw).model_dump()
    assert {
        "client_name", "kind", "year", "month", "month_length",
        "quotation_id", "rows", "days", "cells",
    } <= set(data)

    assert {
        "item_name", "specification", "category",
        "unit_price", "has_price", "usage_qty",
    } <= set(data["rows"][0])

    # Every day, including the untouched EMPTY ones, carries the full shape.
    for day in data["days"]:
        assert {
            "day", "date", "status", "total_qty", "quantities",
            "gate_pass_ids", "delivery_ids", "rewash_ids",
            "confirmed_by", "confirmed_at", "notes",
        } <= set(day)
        assert isinstance(day["gate_pass_ids"], list)
        assert isinstance(day["quantities"], dict)

    # The page reads cells[key][String(day)] and builds its own keys as
    # `name||spec`, so the server pivot must be keyed the same way.
    assert data["cells"]["Duvet Cover||"]["1"] == 50
    assert data["days"][0]["status"] in {"EMPTY", "DRAFT", "CONFIRMED", "CANCELLED"}


async def test_confirmed_day_is_locked(mocked_db):
    await put("receiving", {"Duvet Cover||": 50})
    await confirm("receiving")

    # Re-confirming is a no-op, and a confirmed day cannot be cancelled here:
    # its gate pass is real and has to be reversed through its own flow.
    again = await confirm("receiving")
    assert again["days"]["1"]["status"] == "CONFIRMED"
    assert len(await gate_passes(mocked_db)) == 1

    with pytest.raises(Exception) as exc:
        await m.cancel_monthly_day("receiving", CLIENT, Y, MO, 1, current_user=user())
    assert getattr(exc.value, "status_code", None) == 409

"""Today Sheet API — the web/mobile one-screen commit contract.

These tests prove the sheet is not a parallel accounting system: pending stock
comes from the canonical balance engine, AUTO delivery is FIFO across passes,
and a commit creates ordinary gate passes and ordinary deliveries that every
other screen can read.
"""
from datetime import datetime, timezone

from bson import ObjectId

from bill_service.app_time import parse_wall_clock
from bill_service.crypto_helper import encrypt_dict
from bill_service.models import DeliveryCreate, DeliveryItem
from bill_service.routers import deliveries, today_sheet
from bill_service.services import balance_engine as be
from bill_service.services import operations_context as ctx

from conftest import auth_user

SENSITIVE = ["client_name", "items", "notes"]
TARGET_DAY = "2026-11-23"


def _request():
    return type(
        "Req", (), {"headers": {}, "method": "POST", "url": type("U", (), {"path": "/today-sheet/commit"})()}
    )()


def _gp_item(name, qty, spec=None, unit="pcs"):
    return {
        "item_name": name,
        "category": "Bed",
        "specification": spec,
        "client_qty": qty,
        "received_qty": qty,
        "difference": 0,
        "unit": unit,
        "piece_count": 0,
        "mismatch_reason": None,
        "mismatch_notes": None,
        "rewashed": False,
    }


async def _seed_gate_pass(
    mocked_db,
    *,
    client: str,
    items,
    receiving_date,
    number=None,
    status: str = "RECEIVED",
    received_by: str = "System",
):
    doc = {
        "gate_pass_number": number or f"GP-{ObjectId()}",
        "client_name": client,
        "receiving_date": receiving_date,
        "received_by": received_by,
        "items": items,
        "status": status,
        "notes": "",
        "quotation_id": None,
        "adjustments": [],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await mocked_db["gatepasses_collection"].insert_one(encrypt_dict(doc, SENSITIVE))
    return str(result.inserted_id)


async def _create_delivery(gp_id, client, items, *, day=TARGET_DAY):
    payload = DeliveryCreate(
        gate_pass_id=gp_id,
        client_name=client,
        delivery_date=parse_wall_clock(day),
        delivered_by="Rider",
        received_by="Client rep",
        items=[
            DeliveryItem(item_name=name, specification=spec, quantity=qty, gate_pass_id=gp_id)
            for name, qty, spec in items
        ],
        notes="",
    )
    return await deliveries.create_delivery(payload, current_user=auth_user(), request=_request())


async def _available(gp_id):
    gps = await ctx.load_gate_passes([gp_id])
    dls, returns = await ctx.load_movements([gp_id])
    return ctx.balances_for(gps, ctx.delivered_by_gate_pass(dls), ctx.returned_by_gate_pass(returns))[gp_id]


# ── GET ────────────────────────────────────────────────────────────────────────

async def test_get_today_sheet_returns_pending_stock_and_day_history(mocked_db):
    old = await _seed_gate_pass(
        mocked_db,
        client="Sunshine Hotel",
        items=[_gp_item("Pillow", 10)],
        receiving_date=parse_wall_clock("2026-11-20"),
    )
    newer = await _seed_gate_pass(
        mocked_db,
        client="Sunshine Hotel",
        items=[_gp_item("Pillow", 5)],
        receiving_date=parse_wall_clock("2026-11-21"),
    )
    await _create_delivery(old, "Sunshine Hotel", [("Pillow", 4, None)])
    await _seed_gate_pass(
        mocked_db,
        client="Sunshine Hotel",
        items=[_gp_item("Bath Towel", 8, unit="pcs")],
        receiving_date=parse_wall_clock(TARGET_DAY),
        received_by="Front desk",
    )

    sheet = await today_sheet.get_today_sheet(date=TARGET_DAY, current_user=auth_user())

    assert sheet["date"] == TARGET_DAY
    assert sheet["totals"]["clients"] == 1
    # 10-4 on the old pass, 5 on the newer one, plus 8 received today.
    assert sheet["totals"]["available_qty"] == 19
    client = sheet["clients"][0]
    assert client["client_name"] == "Sunshine Hotel"
    assert len(client["pending_passes"]) == 3
    assert [p["gate_pass_id"] for p in client["pending_passes"][:2]] == [old, newer]
    assert client["receives"][0]["received_by"] == "Front desk"
    assert client["receives"][0]["items"][0]["item_name"] == "Bath Towel"
    assert client["receives"][0]["items"][0]["quantity"] == 8
    assert client["deliveries"][0]["items"][0]["quantity"] == 4
    assert sheet["totals"]["received_qty"] == 8
    assert sheet["totals"]["delivered_qty"] == 4


async def test_get_today_sheet_rejects_bad_date(mocked_db):
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await today_sheet.get_today_sheet(date="23-11-2026", current_user=auth_user())
    assert exc.value.status_code == 400


# ── COMMIT ─────────────────────────────────────────────────────────────────────

async def test_commit_creates_receives_and_fifo_deliveries(mocked_db):
    older = await _seed_gate_pass(
        mocked_db,
        client="Bay Hotel",
        items=[_gp_item("Pillow", 3)],
        receiving_date=parse_wall_clock("2026-11-20"),
        number="GP-20261120-0001",
    )
    newer = await _seed_gate_pass(
        mocked_db,
        client="Bay Hotel",
        items=[_gp_item("Pillow", 4)],
        receiving_date=parse_wall_clock("2026-11-21"),
        number="GP-20261121-0001",
    )

    payload = today_sheet.TodaySheetCommit(
        date=TARGET_DAY,
        receives=[
            today_sheet.TodayReceiveGroup(
                client_name="Garden Hotel",
                received_by="Gate desk",
                items=[today_sheet.TodayReceiveItem(item_name="Bath Towel", quantity=6)],
            )
        ],
        deliveries=[
            today_sheet.TodayDeliveryGroup(
                client_name="Bay Hotel",
                received_by="Bay rep",
                items=[today_sheet.TodayDeliveryItem(item_name="Pillow", quantity=5)],
            )
        ],
    )

    result = await today_sheet.commit_today_sheet(payload, current_user=auth_user())

    assert result["summary"]["failed"] == 0
    assert [r["kind"] for r in result["results"]] == ["receive", "delivery"]
    receive = result["results"][0]
    assert receive["gate_pass_number"].startswith("GP-20261123-")
    assert await mocked_db["gatepasses_collection"].count_documents({}) == 3
    assert await mocked_db["deliveries_collection"].count_documents({}) == 2

    # FIFO: 3 from the older pass, then 2 from the newer one.
    delivery_docs, _returns = await ctx.load_movements()
    by_source = {
        str(d.get("gate_pass_id")): be.as_qty(d["items"][0]["quantity"])
        for d in delivery_docs
    }
    assert by_source == {older: 3, newer: 2}
    assert (await _available(older))["totals"]["outstanding_delivery_qty"] == 0
    assert (await _available(newer))["totals"]["outstanding_delivery_qty"] == 2

    garden = await ctx.load_gate_passes()
    garden_gp = next(gp for gp in garden if gp.get("client_name") == "Garden Hotel")
    assert garden_gp["received_by"] == "Gate desk"
    assert garden_gp["items"][0]["received_qty"] == 6


async def test_commit_reports_row_failure_without_hiding_other_rows(mocked_db):
    await _seed_gate_pass(
        mocked_db,
        client="Bay Hotel",
        items=[_gp_item("Pillow", 2)],
        receiving_date=parse_wall_clock("2026-11-20"),
    )
    payload = today_sheet.TodaySheetCommit(
        date=TARGET_DAY,
        receives=[
            today_sheet.TodayReceiveGroup(
                client_name="Garden Hotel",
                items=[today_sheet.TodayReceiveItem(item_name="Sheet", quantity=1)],
            )
        ],
        deliveries=[
            today_sheet.TodayDeliveryGroup(
                client_name="Bay Hotel",
                items=[today_sheet.TodayDeliveryItem(item_name="Pillow", quantity=3)],
            )
        ],
    )

    result = await today_sheet.commit_today_sheet(payload, current_user=auth_user())

    assert result["summary"]["succeeded"] == 1
    assert result["summary"]["failed"] == 1
    assert result["results"][0]["ok"] is True
    assert result["results"][1]["ok"] is False
    assert "only 2 of 3" in result["results"][1]["error"]
    assert await mocked_db["deliveries_collection"].count_documents({}) == 0


async def test_commit_explicit_gate_pass_is_honoured(mocked_db):
    chosen = await _seed_gate_pass(
        mocked_db,
        client="Bay Hotel",
        items=[_gp_item("Pillow", 5)],
        receiving_date=parse_wall_clock("2026-11-20"),
    )
    await _seed_gate_pass(
        mocked_db,
        client="Bay Hotel",
        items=[_gp_item("Pillow", 5)],
        receiving_date=parse_wall_clock("2026-11-19"),
    )

    payload = today_sheet.TodaySheetCommit(
        date=TARGET_DAY,
        deliveries=[
            today_sheet.TodayDeliveryGroup(
                client_name="Bay Hotel",
                items=[
                    today_sheet.TodayDeliveryItem(
                        item_name="Pillow", quantity=2, gate_pass_id=chosen
                    )
                ],
            )
        ],
    )
    result = await today_sheet.commit_today_sheet(payload, current_user=auth_user())

    assert result["summary"]["failed"] == 0
    doc = await mocked_db["deliveries_collection"].find_one({})
    assert str(doc.get("gate_pass_id")) == chosen
    assert (await _available(chosen))["totals"]["outstanding_delivery_qty"] == 3

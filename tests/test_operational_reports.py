from datetime import date
from unittest.mock import AsyncMock

import pytest

from bill_service.routers import operational_reports
from bill_service.routers.operational_reports import (
    MonthlyOperationsReportRequest,
    _monthly_day_rows,
)


def test_monthly_income_uses_only_confirmed_receiving_lines_and_selected_quote():
    documents = [
        {
            "kind": "receiving",
            "client_name": "Sunrise Hotel",
            "year": 2026,
            "month": 10,
            "days": {
                "10": {
                    "date": "2026-10-10",
                    "status": "CONFIRMED",
                    "quantities": {"Shirt||White": 2, "Towel": 3},
                    "piece_quantities": {"Shirt||White": 2, "Curtain": 1},
                },
                "11": {
                    "date": "2026-10-11",
                    "status": "DRAFT",
                    "quantities": {"Shirt||White": 100},
                },
            },
        },
        {
            "kind": "delivery",
            "client_name": "Sunrise Hotel",
            "year": 2026,
            "month": 10,
            "days": {
                "10": {
                    "date": "2026-10-10",
                    "status": "CONFIRMED",
                    "quantities": {"Shirt||White": 900},
                },
            },
        },
    ]
    price_map = {
        "selected_ids": {"sunrise hotel": "quote-1"},
        "quote-1": {
            ("shirt", "white"): 25,
            ("towel", ""): 40,
            ("curtain", ""): 100,
        },
    }

    report = _monthly_day_rows(documents, date(2026, 10, 1), date(2026, 10, 31), price_map)

    assert report["source_of_truth"] == "monthly_operations_confirmed_receiving"
    assert report["total_income"] == 170.0
    assert report["total_quantity"] == 5.0
    assert report["hotel_summary"] == [{
        "client_name": "Sunrise Hotel",
        "quotation_id": "quote-1",
        "income": 170.0,
        "quantity": 5.0,
        "confirmed_days": 1,
    }]
    pieces_by_item = {item["item_name"]: item["piece_count"] for item in report["daily_details"][0]["items"]}
    assert pieces_by_item["Shirt"] == 2
    curtain = next(item for item in report["daily_details"][0]["items"] if item["item_name"] == "Curtain")
    assert curtain["unit"] == "kg"
    assert curtain["quantity"] == 0.0
    assert curtain["piece_count"] == 1
    assert report["unpriced_count"] == 0


def test_unselected_quotation_marks_receiving_items_unpriced():
    report = _monthly_day_rows(
        [{
            "kind": "receiving",
            "client_name": "Sunrise Hotel",
            "year": 2026,
            "month": 10,
            "days": {
                "10": {
                    "date": "2026-10-10",
                    "status": "CONFIRMED",
                    "quantities": {"Shirt": 4},
                },
            },
        }],
        date(2026, 10, 10),
        date(2026, 10, 10),
        {"selected_ids": {}},
    )

    assert report["total_income"] == 0.0
    assert report["unpriced_count"] == 1
    assert report["unpriced_items"][0]["item_name"] == "Shirt"


@pytest.mark.asyncio
async def test_report_endpoint_uses_monthly_quotation_unless_explicitly_cleared(
    mocked_db, monkeypatch
):
    await mocked_db["monthly_entries_collection"].insert_one({
        "client_name": "Sunrise Hotel",
        "kind": "receiving",
        "year": 2026,
        "month": 10,
        "quotation_id": "quote-1",
        "days": {
            "10": {
                "date": "2026-10-10",
                "status": "CONFIRMED",
                "quantities": {"Shirt": 4},
            },
        },
    })
    monkeypatch.setattr(
        operational_reports,
        "_quotation_prices",
        AsyncMock(return_value={("shirt", ""): 25.0}),
    )
    payload = MonthlyOperationsReportRequest(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
    )

    defaulted = await operational_reports.monthly_operations_report(payload, {"role": "MANAGER"})
    explicitly_unpriced = await operational_reports.monthly_operations_report(
        payload.model_copy(update={"selected_quotations": {"Sunrise Hotel": None}}),
        {"role": "MANAGER"},
    )

    assert defaulted["total_income"] == 100.0
    assert defaulted["hotel_summary"][0]["suggested_quotation_id"] == "quote-1"
    assert explicitly_unpriced["total_income"] == 0.0
    assert explicitly_unpriced["unpriced_count"] == 1


@pytest.mark.asyncio
async def test_report_defaults_to_latest_hotel_quotation(mocked_db, monkeypatch):
    for month, quote_id, quantity in ((9, "old-quote", 1), (10, "new-quote", 2)):
        await mocked_db["monthly_entries_collection"].insert_one({
            "client_name": "Sunrise Hotel",
            "kind": "receiving",
            "year": 2026,
            "month": month,
            "quotation_id": quote_id,
            "days": {
                "1": {
                    "date": f"2026-{month:02d}-01",
                    "status": "CONFIRMED",
                    "quantities": {"Shirt": quantity},
                },
            },
        })
    quote_prices = AsyncMock(return_value={("shirt", ""): 30.0})
    monkeypatch.setattr(operational_reports, "_quotation_prices", quote_prices)

    report = await operational_reports.monthly_operations_report(
        MonthlyOperationsReportRequest(
            start_date=date(2026, 9, 1),
            end_date=date(2026, 10, 31),
        ),
        {"role": "MANAGER"},
    )

    assert quote_prices.await_args_list[0].args == ("new-quote",)
    assert report["selected_quotations"]["Sunrise Hotel"] == "new-quote"
    assert report["total_income"] == 90.0


@pytest.mark.asyncio
async def test_postgresql_style_quote_id_uses_selected_quotation_snapshot(mocked_db, monkeypatch):
    await mocked_db["monthly_entries_collection"].insert_one({
        "client_name": "Sunrise Hotel",
        "kind": "receiving",
        "year": 2026,
        "month": 10,
        "days": {
            "1": {
                "date": "2026-10-01",
                "status": "CONFIRMED",
                "quantities": {"Shirt": 4},
            },
        },
    })
    monkeypatch.setattr(operational_reports, "_quotation_prices", AsyncMock(return_value={}))

    report = await operational_reports.monthly_operations_report(
        MonthlyOperationsReportRequest(
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
            selected_quotations={"Sunrise Hotel": "481"},
            quotation_snapshots={
                "481": {
                    "id": 481,
                    "client_name": "Sunrise Hotel",
                    "line_items": [{"item_name": "Shirt", "unit_price": 25.0}],
                },
            },
        ),
        {"role": "MANAGER"},
    )

    assert report["total_income"] == 100.0
    assert report["quotation_price_sources"]["481"] == "quotation_api_snapshot"
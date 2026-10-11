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
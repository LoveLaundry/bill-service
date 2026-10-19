from datetime import date
from unittest.mock import AsyncMock
from datetime import datetime, timezone

import pytest

from bill_service.routers import operational_reports
from bill_service.routers.operational_reports import (
    MonthlyOperationsReportRequest,
    LinenCategoryMappingPayload,
    _build_gatepass_report,
    _load_category_lookup,
)
from bill_service.services import gate_pass_records
from bill_service.models import GatePassCreate, GatePassItem
from bill_service.crypto_helper import encrypt_dict


@pytest.mark.asyncio
async def test_report_uses_active_gate_passes_and_separates_draft_cancelled(mocked_db, monkeypatch):
    received_at = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
    await mocked_db["gatepasses_collection"].insert_many([
        {
            "client_name": "Sunrise Hotel", "status": "RECEIVED", "receiving_date": received_at,
            "quotation_id": "quote-1", "items": [{"item_name": "Shirt", "received_qty": 4}],
        },
        {
            "client_name": "Sunrise Hotel", "status": "DRAFT", "receiving_date": received_at,
            "quotation_id": "quote-1", "items": [{"item_name": "Shirt", "received_qty": 400}],
        },
        {
            "client_name": "Sunrise Hotel", "status": "CANCELLED", "receiving_date": received_at,
            "quotation_id": "quote-1", "items": [{"item_name": "Shirt", "received_qty": 900}],
        },
    ])
    monkeypatch.setattr(
        operational_reports,
        "_quotation_prices",
        AsyncMock(return_value={("shirt", ""): 25.0}),
    )
    payload = MonthlyOperationsReportRequest(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
    )

    defaulted = await operational_reports.monthly_operations_report(payload, current_user={"role": "MANAGER", "laundry_id": "lovelaundry", "laundries": [{"id": "lovelaundry", "role": "MANAGER", "status": "ACTIVE"}]})
    explicitly_unpriced = await operational_reports.monthly_operations_report(
        payload.model_copy(update={"selected_quotations": {"Sunrise Hotel": None}}),
        current_user={"role": "MANAGER", "laundry_id": "lovelaundry", "laundries": [{"id": "lovelaundry", "role": "MANAGER", "status": "ACTIVE"}]},
    )

    assert defaulted["total_income"] == 100.0
    assert defaulted["hotel_summary"][0]["suggested_quotation_id"] == "quote-1"
    assert defaulted["source_of_truth"] == "active_gate_pass_received_items"
    assert len(defaulted["draft_receiving"]) == 1
    assert defaulted["cancelled_gate_pass_count"] == 1
    assert explicitly_unpriced["total_income"] == 0.0
    assert explicitly_unpriced["unpriced_count"] == 1


@pytest.mark.asyncio
async def test_report_defaults_to_latest_hotel_quotation(mocked_db, monkeypatch):
    for month, quote_id, quantity in ((9, "old-quote", 1), (10, "new-quote", 2)):
        await mocked_db["gatepasses_collection"].insert_one({
            "client_name": "Sunrise Hotel",
            "status": "RECEIVED",
            "receiving_date": datetime(2026, month, 1, 8, tzinfo=timezone.utc),
            "quotation_id": quote_id,
            "items": [{"item_name": "Shirt", "received_qty": quantity}],
        })
    quote_prices = AsyncMock(return_value={("shirt", ""): 30.0})
    monkeypatch.setattr(operational_reports, "_quotation_prices", quote_prices)

    report = await operational_reports.monthly_operations_report(
        MonthlyOperationsReportRequest(
            start_date=date(2026, 9, 1),
            end_date=date(2026, 10, 31),
        ),
        current_user={"role": "MANAGER", "laundry_id": "lovelaundry", "laundries": [{"id": "lovelaundry", "role": "MANAGER", "status": "ACTIVE"}]},
    )

    assert ("new-quote", "lovelaundry") in [call.args for call in quote_prices.await_args_list]
    assert report["selected_quotations"]["Sunrise Hotel"] == "new-quote"
    assert report["total_income"] == 90.0


@pytest.mark.asyncio
async def test_postgresql_style_quote_id_uses_selected_quotation_snapshot(mocked_db, monkeypatch):
    await mocked_db["gatepasses_collection"].insert_one({
        "client_name": "Sunrise Hotel",
        "status": "RECEIVED",
        "receiving_date": datetime(2026, 10, 1, 8, tzinfo=timezone.utc),
        "quotation_id": "481",
        "items": [{"item_name": "Shirt", "received_qty": 4}],
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
        current_user={"role": "MANAGER", "laundry_id": "lovelaundry", "laundries": [{"id": "lovelaundry", "role": "MANAGER", "status": "ACTIVE"}]},
    )

    assert report["total_income"] == 100.0
    assert report["daily_details"][0]["items"][0]["rate_source"] == "selected_current_quotation"


def test_linen_variants_consolidate_without_losing_different_rates():
    gate_passes = [{
        "_id": "gp-1",
        "client_name": "Sunrise Hotel",
        "status": "RECEIVED",
        "receiving_date": datetime(2026, 10, 1, 8, tzinfo=timezone.utc),
        "quotation_id": "quote-1",
        "quotation_price_snapshot": {"prices": {
            "bedsheet - single||": 100,
            "bedsheet - double||": 120,
            "bedsheet - large||": 150,
            "duvet cover - king||": 110,
        }},
        "items": [
            {"item_name": "Bedsheet - Single", "received_qty": 20},
            {"item_name": "Bedsheet - Double", "received_qty": 35},
            {"item_name": "Bedsheet - Large", "received_qty": 15},
            {"item_name": "Duvet Cover - King", "received_qty": 45},
        ],
    }]

    report = _build_gatepass_report(
        gate_passes, [], 0, date(2026, 10, 1), date(2026, 10, 2),
        {}, {}, operational_reports.DEFAULT_CATEGORY_LOOKUP,
    )

    bedsheets = next(row for row in report["period_summary"] if row["category"] == "Bedsheets")
    assert bedsheets["quantity"] == 70.0
    assert bedsheets["value"] == 8450.0
    assert bedsheets["rate_label"] == "Multiple Rates"
    assert len(bedsheets["variants"]) == 3
    assert report["daily_summary"][1]["value"] == 0.0
    assert report["daily_summary"][0]["transaction_count"] == 1
    assert report["total_income"] == 13400.0


def test_unknown_items_remain_unclassified_and_rewash_is_excluded():
    report = _build_gatepass_report(
        [{
            "_id": "gp-1",
            "client_name": "Sunrise Hotel",
            "status": "RECEIVED",
            "receiving_date": datetime(2026, 10, 1, 8, tzinfo=timezone.utc),
            "items": [
                {"item_name": "Custom Laundry Bag", "received_qty": 3},
                {"item_name": "Bedsheet - Single", "received_qty": 5, "rewashed": True},
            ],
        }],
        [], 0, date(2026, 10, 1), date(2026, 10, 1), {}, {},
        operational_reports.DEFAULT_CATEGORY_LOOKUP,
    )

    assert report["unmapped_items"][0]["item_name"] == "Custom Laundry Bag"
    assert report["unmapped_items"][0]["category"] == "Other / Unclassified Items"
    assert report["total_quantity"] == 3.0


@pytest.mark.asyncio
async def test_admin_mapping_classifies_unknown_item_and_is_audited(mocked_db, monkeypatch):
    monkeypatch.setattr(operational_reports, "audit_collection", mocked_db["audit_collection"])
    await operational_reports.upsert_linen_category_mapping(
        LinenCategoryMappingPayload(item_name="Custom Bath Set", category="Bath Towels"),
        {"auth_id": "admin-1", "role": "ADMIN"},
    )

    lookup = await _load_category_lookup()
    audit_rows = await mocked_db["audit_collection"].find({}).to_list(length=None)
    report = _build_gatepass_report(
        [{
            "_id": "gp-custom",
            "client_name": "Sunrise Hotel",
            "status": "RECEIVED",
            "receiving_date": datetime(2026, 10, 1, 8, tzinfo=timezone.utc),
            "quotation_price_snapshot": {"prices": {"custom bath set||": 50}},
            "items": [{"item_name": "Custom Bath Set", "received_qty": 2}],
        }],
        [], 0, date(2026, 10, 1), date(2026, 10, 1), {}, {}, lookup,
    )

    assert lookup["custom bath set"] == "Bath Towels"
    assert report["unmapped_items"] == []
    assert next(row for row in report["period_summary"] if row["category"] == "Bath Towels")["quantity"] == 2
    assert audit_rows[0]["action"] == "LINEN_CATEGORY_MAPPING_UPSERT"


@pytest.mark.asyncio
async def test_report_decrypts_encrypted_gate_passes(mocked_db, monkeypatch):
    gp = {
        "client_name": "Avenra Garden Hotel",
        "status": "RECEIVED",
        "receiving_date": datetime(2026, 10, 3, 8, tzinfo=timezone.utc),
        "quotation_id": "quote-1",
        "items": [{"item_name": "Shirt", "received_qty": 4}],
    }
    await mocked_db["gatepasses_collection"].insert_one(
        encrypt_dict(gp, ["client_name", "items", "notes"])
    )
    monkeypatch.setattr(
        operational_reports,
        "_quotation_prices",
        AsyncMock(return_value={("shirt", ""): 25.0}),
    )

    report = await operational_reports.monthly_operations_report(
        MonthlyOperationsReportRequest(
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
        ),
        current_user={"role": "MANAGER", "laundry_id": "lovelaundry", "laundries": [{"id": "lovelaundry", "role": "MANAGER", "status": "ACTIVE"}]},
    )

    assert report["total_income"] == 100.0
    assert report["hotel_summary"][0]["client_name"] == "Avenra Garden Hotel"


@pytest.mark.asyncio
async def test_gate_pass_creation_persists_quotation_price_snapshot(mocked_db, monkeypatch):
    snapshot = {"bedsheet - single||": 100.0}
    monkeypatch.setattr(gate_pass_records, "_quotation_price_snapshot", AsyncMock(return_value=snapshot))

    await gate_pass_records.create_gate_pass_record(
        GatePassCreate(
            gate_pass_number="GP-20261001-9001",
            client_name="Sunrise Hotel",
            receiving_date=datetime(2026, 10, 1, 8, tzinfo=timezone.utc),
            received_by="Tester",
            quotation_id="65f0aa0000000000000000aa",
            items=[GatePassItem(
                item_name="Bedsheet - Single",
                client_qty=2,
                received_qty=2,
            )],
        ),
        {"auth_id": "admin-1", "user_name": "Admin"},
        laundry_id="lovelaundry",
    )
    stored = await mocked_db["gatepasses_collection"].find_one({"gate_pass_number": "GP-20261001-9001"})

    assert stored["quotation_price_snapshot"]["prices"] == snapshot
    assert stored["quotation_price_snapshot"]["quotation_id"] == "65f0aa0000000000000000aa"
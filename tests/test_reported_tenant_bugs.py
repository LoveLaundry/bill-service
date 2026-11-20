"""Regressions for the tenant-scoping bugs reported as "system malfunctioning".

Each test seeds two laundries with identically-named clients so that any
unscoped query silently *merges* another tenant's data into the caller's
totals instead of returning an obviously-foreign row.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from bill_service import crypto_helper
from bill_service.database import connection_manager
from bill_service.models import MONTHLY_KINDS, ShopBillCreate
from bill_service.routers import dashboard, monthly, shop_bills
from conftest import LAUNDRY_A, LAUNDRY_B, tenant_user


def _now():
    return datetime.now(timezone.utc)


def ua():
    return tenant_user("tester-a", LAUNDRY_A, "ADMIN")


def _gp(laundry_id, qty):
    return crypto_helper.encrypt_dict(
        {
            "laundry_id": laundry_id,
            "gate_pass_number": f"GP-{laundry_id}",
            "client_name": "Hotel X",
            "status": "RECEIVED",
            "receiving_date": _now().isoformat(),
            "items": [{
                "item_name": "Shirt", "specification": None, "category": "Wash",
                "client_qty": qty, "received_qty": qty, "difference": 0, "rewashed": False,
            }],
            "notes": "",
            "created_at": _now(),
            "updated_at": _now(),
        },
        dashboard.SENSITIVE_FIELDS_GP,
    )


async def test_today_deliveries_does_not_merge_other_laundry(mocked_db):
    for laundry_id, qty in ((LAUNDRY_A, 5), (LAUNDRY_B, 7)):
        await mocked_db["deliveries_collection"].insert_one(crypto_helper.encrypt_dict(
            {
                "laundry_id": laundry_id,
                "client_name": "Hotel X",
                "status": "DELIVERED",
                "items": [{"item_name": "Shirt", "specification": None, "quantity": qty}],
                "created_at": _now(),
                "updated_at": _now(),
            },
            dashboard.SENSITIVE_FIELDS_DEL,
        ))

    result = await dashboard.today_deliveries(current_user=ua())

    assert [(r["client_name"], r["total_qty"]) for r in result] == [("Hotel X", 5)]


async def test_client_wise_report_ignores_other_laundry_bills(mocked_db):
    await mocked_db["gatepasses_collection"].insert_one(_gp(LAUNDRY_A, 4))
    await mocked_db["gatepasses_collection"].insert_one(_gp(LAUNDRY_B, 6))
    for laundry_id, total in ((LAUNDRY_A, 100.0), (LAUNDRY_B, 900.0)):
        await mocked_db["bills_collection"].insert_one(crypto_helper.encrypt_dict(
            {
                "laundry_id": laundry_id,
                "client_name": "Hotel X",
                "grand_total": total,
                "paid_amount": 0.0,
                "outstanding_amount": total,
                "payment_status": "PENDING",
                "items": [],
                "created_at": _now(),
            },
            dashboard.SENSITIVE_FIELDS_BILL,
        ))

    result = await dashboard.get_client_wise_report(current_user=ua())

    assert len(result) == 1
    assert result[0]["total_received"] == 4
    assert result[0]["total_billed"] == 100.0


async def test_bulk_monthly_sync_only_touches_own_hotels(mocked_db):
    for laundry_id, hotel in ((LAUNDRY_A, "Alpha Hotel"), (LAUNDRY_B, "Beta Hotel")):
        for kind in MONTHLY_KINDS:
            await mocked_db["monthly_entries_collection"].insert_one(crypto_helper.encrypt_dict(
                {
                    "laundry_id": laundry_id,
                    "client_name": hotel,
                    "kind": kind,
                    "year": 2026,
                    "month": 9,
                    "days": {},
                    "created_at": _now(),
                    "updated_at": _now(),
                },
                monthly.MONTH_SENSITIVE_FIELDS,
            ))

    results = await monthly.trigger_sync_all(
        monthly._SyncAllPayload(year=2026, month=9), current_user=ua()
    )

    assert {row["hotel"] for row in results} == {"Alpha Hotel"}
    assert {row["kind"] for row in results} == set(MONTHLY_KINDS)


async def test_shop_bill_dashboard_summary_is_tenant_scoped(mocked_db):
    for laundry_id, total in ((LAUNDRY_A, 100.0), (LAUNDRY_B, 900.0)):
        await mocked_db["shop_bills_collection"].insert_one(crypto_helper.encrypt_dict(
            {
                "laundry_id": laundry_id,
                "client_name": "Hotel X",
                "grand_total": total,
                "paid_amount": 0.0,
                "outstanding_amount": total,
                "status": "ISSUED",
                "payment_status": "PENDING",
                "items": [],
                "created_at": _now(),
            },
            shop_bills.SENSITIVE_FIELDS,
        ))

    summary = await shop_bills.dashboard_summary(days=30, current_user=ua())

    assert summary["total_bills"] == 1
    assert summary["total_revenue"] == 100.0
    assert summary["outstanding"] == 100.0


async def test_create_bill_returns_created_document(mocked_db):
    payload = ShopBillCreate(
        client_name="Hotel X",
        items=[{"item_name": "Shirt", "unit_price": 10.0, "quantity": 2}],
    )

    created = await shop_bills.create_bill(payload, current_user=ua())

    assert created["client_name"] == "Hotel X"
    assert created["grand_total"] == 20.0
    raw = await mocked_db["shop_bills_collection"].find_one({"_id": ObjectId(created["id"])})
    assert raw["laundry_id"] == LAUNDRY_A


async def test_from_quotation_is_tenant_scoped(mocked_db, monkeypatch):
    fake_client = AsyncMongoMockClient()
    quotes = fake_client["quotations_db"]["quotations"]
    own = await quotes.insert_one({
        "_id": ObjectId(), "laundry_id": LAUNDRY_A, "client_name": "Hotel X",
        "items": [{"item_name": "Shirt", "unit_price": 10.0, "quantity": 2}],
    })
    foreign = await quotes.insert_one({
        "_id": ObjectId(), "laundry_id": LAUNDRY_B, "client_name": "Hotel Y",
        "items": [{"item_name": "Trouser", "unit_price": 99.0, "quantity": 1}],
    })
    monkeypatch.setattr(connection_manager, "get_client", lambda _role: fake_client)

    created = await shop_bills.from_quotation(str(own.inserted_id), current_user=ua())
    assert created["client_name"] == "Hotel X"
    assert created["laundry_id"] == LAUNDRY_A

    with pytest.raises(Exception) as exc:
        await shop_bills.from_quotation(str(foreign.inserted_id), current_user=ua())
    assert getattr(exc.value, "status_code", None) == 404

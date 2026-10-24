"""Hotel client portal: isolation, money arithmetic, and the negative contract.

The portal is the only surface an external hotel can reach. These tests assert
that it shows exactly one hotel's records, that money comes from stored values
rather than client-side addition, and that a guest of hotel A can never read a
document of hotel B — including by guessing a record id.
"""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from bill_service.crypto_helper import encrypt_dict, get_search_token
from bill_service.database import main_db
from bill_service.routers import client_portal
from bill_service.routers.client_portal import (
    BILL_SENSITIVE,
    DELIVERY_SENSITIVE,
    GP_SENSITIVE,
    resolve_client_scope,
)
from bill_service.tenancy import resolve_laundry_id

LAUNDRY = "lovelaundry"
OTHER_LAUNDRY = "rivalwash"
HOTEL_A = "Cinnamon Grand"
HOTEL_B = "Galle Face Hotel"


def client_session(hotel=HOTEL_A, laundry=LAUNDRY):
    """The exact claim shape user-service issues for a hotel login."""
    return {
        "auth_id": "cinnamon",
        "user_name": "Front Desk",
        "role": "HOTEL_MANAGER",
        "platform_admin": False,
        "token_kind": "client",
        "client_scope": {"laundry_id": laundry, "client_name": hotel},
    }


def staff_session(laundry=LAUNDRY):
    return {
        "auth_id": "asha",
        "role": "ADMIN",
        "laundry_id": laundry,
        "laundries": [{"id": laundry, "name": "Love Laundry", "role": "ADMIN", "status": "ACTIVE"}],
    }


def _now():
    return datetime.now(timezone.utc)


async def seed_bill(
    mocked_db,
    *,
    hotel,
    laundry=LAUNDRY,
    grand=1000.0,
    paid=0.0,
    status="PENDING",
    created=None,
    number="B-1",
):
    doc = {
        "laundry_id": laundry,
        "client_name": hotel,
        "client_name_search": get_search_token(hotel),
        "manual_bill_number": number,
        "quotation_title": "Monthly linen",
        "items": [{"item_name": "Sheet", "quantity": 10, "amount": grand}],
        "total_quantity": 10,
        "total_amount": grand,
        "discounts": 0,
        "transport_fee": 0,
        "taxes": 0,
        "additional_charges": 0,
        "grand_total": grand,
        "paid_amount": paid,
        "payment_status": status,
        "created_at": created or _now(),
        "notes": "",
    }
    encrypted = encrypt_dict(doc, BILL_SENSITIVE)
    result = await main_db.bills_collection.insert_one(encrypted)
    return result.inserted_id


async def seed_gate_pass(mocked_db, *, hotel, laundry=LAUNDRY, status="RECEIVED", number="GP-1"):
    doc = {
        "laundry_id": laundry,
        "gate_pass_number": number,
        "client_name": hotel,
        "client_name_search": get_search_token(hotel),
        "receiving_date": _now().isoformat(),
        "received_by": "Driver",
        "status": status,
        "items": [
            {
                "item_name": "Pillow",
                "specification": None,
                "category": "Bed",
                "client_qty": 4,
                "received_qty": 4,
                "difference": 0,
                "rewashed": False,
            }
        ],
        "notes": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    encrypted = encrypt_dict(doc, GP_SENSITIVE)
    await main_db.gatepasses_collection.insert_one(encrypted)


async def seed_delivery(mocked_db, *, hotel, laundry=LAUNDRY, status="DELIVERED"):
    doc = {
        "laundry_id": laundry,
        "gate_pass_id": "gp-1",
        "source_gate_pass_ids": ["gp-1"],
        "client_name": hotel,
        "client_name_search": get_search_token(hotel),
        "delivery_date": _now(),
        "delivered_by": "Driver",
        "received_by": "Housekeeping",
        "items": [],
        "status": status,
        "corrections": [],
        "created_at": _now(),
    }
    encrypted = encrypt_dict(doc, DELIVERY_SENSITIVE)
    await main_db.deliveries_collection.insert_one(encrypted)


# ── the scope dependency ─────────────────────────────────────────────────────

def test_client_scope_is_extracted():
    scope = resolve_client_scope(current_user=client_session())
    assert scope == {"laundry_id": LAUNDRY, "client_name": HOTEL_A, "client_type": "HOTEL"}


def test_scope_normalises_the_laundry_id():
    scope = resolve_client_scope(current_user=client_session(laundry="LoveLaundry"))
    assert scope["laundry_id"] == "lovelaundry"


def test_staff_token_cannot_use_the_portal():
    with pytest.raises(HTTPException) as excinfo:
        resolve_client_scope(current_user=staff_session())
    assert excinfo.value.status_code == 403


def test_incomplete_scope_is_refused():
    broken = client_session()
    broken["client_scope"] = {"laundry_id": LAUNDRY, "client_name": "   "}
    with pytest.raises(HTTPException) as excinfo:
        resolve_client_scope(current_user=broken)
    assert excinfo.value.status_code == 403


def test_client_token_cannot_reach_staff_endpoints():
    """The tenant resolver every staff endpoint uses must reject a hotel token."""
    with pytest.raises(HTTPException) as excinfo:
        resolve_laundry_id(client_session())
    assert excinfo.value.status_code in (400, 403)


# ── isolation ────────────────────────────────────────────────────────────────

async def test_summary_counts_only_this_hotel(mocked_db):
    await seed_bill(mocked_db, hotel=HOTEL_A, grand=800, paid=300, status="PARTIALLY_PAID")
    await seed_bill(mocked_db, hotel=HOTEL_B, grand=9999, paid=0, status="PENDING")
    # Same hotel name, different laundry: must not leak in.
    await seed_bill(mocked_db, hotel=HOTEL_A, laundry=OTHER_LAUNDRY, grand=5555, status="PENDING")

    summary = await client_portal.portal_summary(scope=resolve_client_scope(client_session()))

    assert summary["hotel"] == HOTEL_A
    assert summary["outstanding_amount"] == 500.0
    assert summary["totals_truncated"] is False


async def test_open_gate_passes_excludes_completed_work(mocked_db):
    await seed_gate_pass(mocked_db, hotel=HOTEL_A, status="RECEIVED", number="GP-1")
    await seed_gate_pass(mocked_db, hotel=HOTEL_A, status="PROCESSING", number="GP-2")
    await seed_gate_pass(mocked_db, hotel=HOTEL_A, status="DELIVERED", number="GP-3")
    await seed_gate_pass(mocked_db, hotel=HOTEL_B, status="RECEIVED", number="GP-4")

    summary = await client_portal.portal_summary(scope=resolve_client_scope(client_session()))
    assert summary["open_gate_passes"] == 2


async def test_gate_pass_list_is_scoped_and_decrypted(mocked_db):
    await seed_gate_pass(mocked_db, hotel=HOTEL_A, number="GP-A")
    await seed_gate_pass(mocked_db, hotel=HOTEL_B, number="GP-B")

    result = await client_portal.portal_gate_passes(status=None, skip=0, limit=50, scope=resolve_client_scope(client_session()))

    assert result["total"] == 1
    assert [item["gate_pass_number"] for item in result["items"]] == ["GP-A"]
    # Decrypted for the hotel, not returned as ciphertext.
    assert result["items"][0]["client_name"] == HOTEL_A


async def test_bill_list_reports_balance_from_stored_values(mocked_db):
    await seed_bill(mocked_db, hotel=HOTEL_A, grand=1250.5, paid=250.5, status="PARTIALLY_PAID")
    await seed_bill(mocked_db, hotel=HOTEL_B, grand=100, status="PENDING")

    result = await client_portal.portal_bills(payment_status=None, skip=0, limit=50, scope=resolve_client_scope(client_session()))

    assert result["total"] == 1
    bill = result["items"][0]
    assert bill["grand_total"] == 1250.5
    assert bill["balance"] == 1000.0


async def test_another_hotels_invoice_cannot_be_opened_by_id(mocked_db):
    foreign_id = await seed_bill(mocked_db, hotel=HOTEL_B, number="B-SECRET")

    with pytest.raises(HTTPException) as excinfo:
        await client_portal.portal_bill_detail(
            bill_id=str(foreign_id), scope=resolve_client_scope(client_session())
        )
    # 404, not 403: an id must not be confirmable from outside the account.
    assert excinfo.value.status_code == 404


async def test_own_invoice_opens_with_full_breakdown(mocked_db):
    own_id = await seed_bill(mocked_db, hotel=HOTEL_A, grand=750, paid=0, status="ISSUED")

    detail = await client_portal.portal_bill_detail(
        bill_id=str(own_id), scope=resolve_client_scope(client_session())
    )

    assert detail["grand_total"] == 750.0
    assert detail["balance"] == 750.0
    assert detail["payment_status"] == "ISSUED"


async def test_cancelled_bills_do_not_count_as_outstanding(mocked_db):
    await seed_bill(mocked_db, hotel=HOTEL_A, grand=900, status="CANCELLED")
    summary = await client_portal.portal_summary(scope=resolve_client_scope(client_session()))
    assert summary["outstanding_amount"] == 0.0


async def test_deliveries_are_scoped_to_the_hotel(mocked_db):
    await seed_delivery(mocked_db, hotel=HOTEL_A)
    await seed_delivery(mocked_db, hotel=HOTEL_B)

    result = await client_portal.portal_deliveries(status=None, skip=0, limit=50, scope=resolve_client_scope(client_session()))
    assert result["total"] == 1
    assert result["items"][0]["client_name"] == HOTEL_A

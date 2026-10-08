"""Legacy invoices are mutable until they are marked PAID.

A legacy invoice aggregates old paper bills into a durable record. Until it is
settled the accountant must be able to fix a shop name, a date or an amount;
once ``mark-paid`` is called the record is final — editing and deleting are
rejected so the paid figure can never silently change.
"""
import pytest
from fastapi import HTTPException

from bill_service.models import LegacyInvoiceCreate, LegacyInvoiceEntry
from bill_service.routers.shop_bills import (
    create_legacy_invoice,
    delete_legacy_invoice,
    get_legacy_invoice,
    list_legacy_invoices,
    mark_legacy_invoice_paid,
    update_legacy_invoice,
)

SHOP = "Sunny Hotel"
ENTRIES = [
    LegacyInvoiceEntry(date="2026-08-01", bill_number="B-001", amount=120.5),
    LegacyInvoiceEntry(date="2026-08-03", bill_number="B-002", amount=80),
]


def user(name="Alice"):
    return {"auth_id": f"u-{name}", "user_name": name, "role": "ADMIN"}


def create_payload(shop=SHOP, entries=None):
    return LegacyInvoiceCreate(shop_name=shop, entries=entries or list(ENTRIES))


async def make_invoice(mocked_db):
    inv = await create_legacy_invoice(create_payload(), current_user=user())
    return inv


async def test_new_invoice_starts_pending(mocked_db):
    inv = await make_invoice(mocked_db)
    assert inv["payment_status"] == "PENDING"
    assert inv["grand_total"] == 200.5
    assert inv["total_entries"] == 2


async def test_update_keeps_number_and_replaces_fields(mocked_db):
    inv = await make_invoice(mocked_db)
    updated = await update_legacy_invoice(
        inv["id"],
        create_payload(shop="Sunny Hotel (Renamed)", entries=[LegacyInvoiceEntry(bill_number="B-010", amount=300)]),
        current_user=user(),
    )
    assert updated["invoice_number"] == inv["invoice_number"]
    assert updated["shop_name"] == "Sunny Hotel (Renamed)"
    assert updated["grand_total"] == 300
    assert updated["total_entries"] == 1
    assert updated["payment_status"] == "PENDING"

    fetched = await get_legacy_invoice(updated["id"], current_user=user())
    assert fetched["grand_total"] == 300
    assert fetched["entries"][0]["bill_number"] == "B-010"


async def test_mark_paid_is_idempotent(mocked_db):
    inv = await make_invoice(mocked_db)
    paid = await mark_legacy_invoice_paid(inv["id"], current_user=user())
    assert paid["payment_status"] == "PAID"
    again = await mark_legacy_invoice_paid(paid["id"], current_user=user())
    assert again["payment_status"] == "PAID"


async def test_paid_invoice_cannot_be_edited(mocked_db):
    inv = await make_invoice(mocked_db)
    await mark_legacy_invoice_paid(inv["id"], current_user=user())
    with pytest.raises(HTTPException) as exc:
        await update_legacy_invoice(inv["id"], create_payload(shop="Changed"), current_user=user())
    assert exc.value.status_code == 409


async def test_paid_invoice_cannot_be_deleted(mocked_db):
    inv = await make_invoice(mocked_db)
    await mark_legacy_invoice_paid(inv["id"], current_user=user())
    with pytest.raises(HTTPException) as exc:
        await delete_legacy_invoice(inv["id"], current_user=user())
    assert exc.value.status_code == 409


async def test_unpaid_invoice_can_be_deleted(mocked_db):
    inv = await make_invoice(mocked_db)
    result = await delete_legacy_invoice(inv["id"], current_user=user())
    assert result["message"] == "Legacy invoice deleted"


async def test_update_rejects_zero_total(mocked_db):
    inv = await make_invoice(mocked_db)
    with pytest.raises(HTTPException) as exc:
        await update_legacy_invoice(
            inv["id"],
            create_payload(entries=[LegacyInvoiceEntry(bill_number="B-000", amount=0)]),
            current_user=user(),
        )
    assert exc.value.status_code == 400


async def test_update_keeps_audit_fields(mocked_db):
    inv = await make_invoice(mocked_db)
    updated = await update_legacy_invoice(inv["id"], create_payload(), current_user=user())
    assert updated["invoice_number"] == inv["invoice_number"]
    assert updated["created_by"] == inv["created_by"]
    persisted = await get_legacy_invoice(inv["id"], current_user=user())
    assert persisted["created_at"] == updated["created_at"]
    assert persisted["grand_total"] == updated["grand_total"]


async def test_list_sorts_by_invoice_number_and_date(mocked_db):
    first = await make_invoice(mocked_db)
    second = await make_invoice(mocked_db)

    result = await list_legacy_invoices(skip=0, limit=20, search=None, sort_by="invoice_number", sort_dir="asc", current_user=user())
    numbers = [item["invoice_number"] for item in result["items"]]
    assert numbers == sorted(numbers)
    assert {first["invoice_number"], second["invoice_number"]}.issubset(numbers)

    result_desc = await list_legacy_invoices(skip=0, limit=20, search=None, sort_by="created_at", sort_dir="desc", current_user=user())
    dates = [item["created_at"] for item in result_desc["items"]]
    assert dates == sorted(dates, reverse=True)

    with pytest.raises(HTTPException) as exc:
        await list_legacy_invoices(skip=0, limit=20, search=None, sort_by="shop_name", current_user=user())
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        await list_legacy_invoices(skip=0, limit=20, search=None, sort_by="created_at", sort_dir="sideways", current_user=user())
    assert exc.value.status_code == 422
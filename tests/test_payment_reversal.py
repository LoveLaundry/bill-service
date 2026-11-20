"""Reversing a payment must move the money back, exactly once.

The dangerous outcomes are a double subtraction (two reversals racing) and a
subtraction with no record (bill unwound, payment row untouched). Both are
pinned here.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import encrypt_dict, get_search_token
from bill_service.database import main_db
from bill_service.routers.bills import PaymentReverse, reverse_payment
from conftest import LAUNDRY_A, tenant_user

BILL_SENSITIVE = ["client_name", "quotation_title", "notes", "items"]
PAYMENT_SENSITIVE = ["client_name", "notes"]


def _now():
    return datetime.now(timezone.utc)


async def _seed(grand_total=250.0, paid=100.0, status="PARTIALLY_PAID", bill_status="ACTIVE"):
    bill = encrypt_dict(
        {
            "laundry_id": LAUNDRY_A,
            "quotation_id": "q-reversal",
            "client_name": "Reversal Client",
            "client_name_search": get_search_token("Reversal Client"),
            "items": [],
            "total_quantity": 10,
            "total_amount": grand_total,
            "grand_total": grand_total,
            "payment_status": status,
            "paid_amount": paid,
            "outstanding_amount": round(grand_total - paid, 2),
            "status": bill_status,
            "created_at": _now(),
            "updated_at": _now(),
        },
        BILL_SENSITIVE,
    )
    bill_id = (await main_db.bills_collection.insert_one(bill)).inserted_id

    payment = encrypt_dict(
        {
            "laundry_id": LAUNDRY_A,
            "bill_id": str(bill_id),
            "client_name": "Reversal Client",
            "client_name_search": get_search_token("Reversal Client"),
            "amount": paid,
            "payment_method": "CASH",
            "payment_date": _now(),
            "reference": "RCPT-1",
            "notes": None,
            "created_at": _now(),
        },
        PAYMENT_SENSITIVE,
    )
    payment_id = (await main_db.payments_collection.insert_one(payment)).inserted_id
    return bill_id, payment_id


async def test_a_reversal_returns_the_balance(mocked_db):
    bill_id, payment_id = await _seed()

    result = await reverse_payment(
        str(payment_id), PaymentReverse(reason="duplicate receipt"), current_user=tenant_user()
    )

    assert result["reversed"] is True
    assert result["amount"] == 100.0
    assert result["bill"]["paid_amount"] == 0.0
    assert result["bill"]["outstanding_amount"] == 250.0
    assert result["bill"]["payment_status"] == "PENDING"

    stored = await main_db.bills_collection.find_one({"_id": bill_id})
    assert stored["paid_amount"] == 0.0


async def test_the_payment_row_records_who_reversed_it(mocked_db):
    _, payment_id = await _seed()

    await reverse_payment(
        str(payment_id), PaymentReverse(reason="bank bounce"), current_user=tenant_user("finance")
    )

    stored = await main_db.payments_collection.find_one({"_id": payment_id})
    assert stored["reversed_at"] is not None
    assert stored["reversed_by"] == "finance"
    assert stored["reversal_reason"] == "bank bounce"


async def test_a_payment_cannot_be_reversed_twice(mocked_db):
    _, payment_id = await _seed()

    await reverse_payment(str(payment_id), PaymentReverse(), current_user=tenant_user())

    with pytest.raises(HTTPException) as caught:
        await reverse_payment(str(payment_id), PaymentReverse(), current_user=tenant_user())
    assert caught.value.status_code == 409

    bill = await main_db.bills_collection.find_one({})
    # Still zero: the second attempt must not subtract 100 again.
    assert bill["paid_amount"] == 0.0


async def test_a_cancelled_bill_refuses_reversal(mocked_db):
    _, payment_id = await _seed(bill_status="CANCELLED")

    with pytest.raises(HTTPException) as caught:
        await reverse_payment(str(payment_id), PaymentReverse(), current_user=tenant_user())
    assert caught.value.status_code == 409


async def test_an_unknown_payment_is_a_404(mocked_db):
    with pytest.raises(HTTPException) as caught:
        await reverse_payment(str(ObjectId()), PaymentReverse(), current_user=tenant_user())
    assert caught.value.status_code == 404

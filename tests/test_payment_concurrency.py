"""Route-level regression tests for payment balance reservations."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import decrypt_dict, encrypt_dict
from bill_service.models import PaymentCreate
from bill_service.routers import bills

BILL_SENSITIVE_FIELDS = ["client_name", "quotation_title", "notes", "items"]
PAYMENT_SENSITIVE_FIELDS = ["client_name", "notes"]


def _now():
    return datetime.now(timezone.utc)


async def _seed_bill(db, grand_total=100.0):
    now = _now()
    result = await db["bills_collection"].insert_one(
        encrypt_dict(
            {
                "quotation_id": "q-payment-test",
                "client_name": "Payment Test Client",
                "items": [],
                "total_quantity": 0,
                "total_amount": grand_total,
                "grand_total": grand_total,
                "payment_status": "PENDING",
                "paid_amount": 0.0,
                "outstanding_amount": grand_total,
                "created_at": now,
                "updated_at": now,
            },
            BILL_SENSITIVE_FIELDS,
        )
    )
    return str(result.inserted_id)


def _request():
    return SimpleNamespace(headers={})


def _payment(amount):
    return PaymentCreate(
        amount=amount,
        payment_method="CASH",
        payment_date=_now(),
        reference=None,
        notes=None,
    )


async def _post_payment(bill_id, amount):
    return await bills.create_payment(
        bill_id,
        _payment(amount),
        current_user={"auth_id": "payment-test-user", "user_name": "Tester"},
        request=_request(),
    )


async def _read_bill(db, bill_id):
    raw = await db["bills_collection"].find_one({"_id": ObjectId(bill_id)})
    return decrypt_dict(raw, BILL_SENSITIVE_FIELDS)


async def test_concurrent_posts_cannot_overpay_and_records_match_aggregate(mocked_db):
    bill_id = await _seed_bill(mocked_db)

    results = await asyncio.gather(
        _post_payment(bill_id, 60.0),
        _post_payment(bill_id, 60.0),
        return_exceptions=True,
    )

    successful = [result for result in results if not isinstance(result, Exception)]
    rejected = [result for result in results if isinstance(result, HTTPException)]
    assert len(successful) == 1
    assert len(rejected) == 1
    assert rejected[0].status_code == 400

    bill = await _read_bill(mocked_db, bill_id)
    payments = [
        decrypt_dict(payment, PAYMENT_SENSITIVE_FIELDS)
        async for payment in mocked_db["payments_collection"].find(
            {"bill_id": bill_id}
        )
    ]
    recorded_total = round(sum(payment["amount"] for payment in payments), 2)
    assert len(payments) == 1
    assert bill["paid_amount"] == recorded_total == 60.0
    assert bill["outstanding_amount"] == 40.0
    assert bill["payment_status"] == "PARTIALLY_PAID"


async def test_concurrent_valid_posts_accumulate_without_stale_overwrite(mocked_db):
    bill_id = await _seed_bill(mocked_db)

    results = await asyncio.gather(
        _post_payment(bill_id, 30.0),
        _post_payment(bill_id, 40.0),
        return_exceptions=True,
    )

    assert all(not isinstance(result, Exception) for result in results)
    bill = await _read_bill(mocked_db, bill_id)
    payments = [
        decrypt_dict(payment, PAYMENT_SENSITIVE_FIELDS)
        async for payment in mocked_db["payments_collection"].find(
            {"bill_id": bill_id}
        )
    ]
    recorded_total = round(sum(payment["amount"] for payment in payments), 2)
    assert len(payments) == 2
    assert bill["paid_amount"] == recorded_total == 70.0
    assert bill["outstanding_amount"] == 30.0
    assert bill["payment_status"] == "PARTIALLY_PAID"


async def test_overpayment_is_rejected_without_a_payment_record(mocked_db):
    bill_id = await _seed_bill(mocked_db)

    with pytest.raises(HTTPException) as exc:
        await _post_payment(bill_id, 100.02)

    assert exc.value.status_code == 400
    assert "exceeds outstanding" in exc.value.detail
    bill = await _read_bill(mocked_db, bill_id)
    assert bill["paid_amount"] == 0.0
    assert bill["outstanding_amount"] == 100.0
    assert await mocked_db["payments_collection"].count_documents(
        {"bill_id": bill_id}
    ) == 0


async def test_failed_payment_insert_compensates_bill_reservation(mocked_db, monkeypatch):
    bill_id = await _seed_bill(mocked_db)
    payments = bills.payments_collection

    class FailingPaymentCollection:
        async def find_one(self, query):
            return await payments.find_one(query)

        async def insert_one(self, _doc):
            raise RuntimeError("simulated payment collection failure")

    monkeypatch.setattr(bills, "payments_collection", FailingPaymentCollection())

    with pytest.raises(HTTPException) as exc:
        await _post_payment(bill_id, 30.0)

    assert exc.value.status_code == 500
    assert "bill balance was restored" in exc.value.detail
    bill = await _read_bill(mocked_db, bill_id)
    assert bill["paid_amount"] == 0.0
    assert bill["outstanding_amount"] == 100.0
    assert bill["payment_status"] == "PENDING"
    assert await payments.count_documents({"bill_id": bill_id}) == 0

"""Regression tests for the money side of a correction.

A balance correction is a piece count and never moves money, but an APPROVED
received-quantity correction does — through the bill re-sync. These tests pin the
two ways that re-sync used to leave a bill internally inconsistent: the status
label stayed behind the money, and a payment cap was read from a cached figure
that a correction had already moved.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import decrypt_dict, encrypt_dict
from bill_service.routers import bills as bills_router
from bill_service.services import bill_sync

BILL_SENSITIVE = bill_sync.BILL_SENSITIVE_FIELDS


def _now():
    return datetime.now(timezone.utc)


async def _store_bill(mocked_db, doc: dict) -> str:
    res = await mocked_db["bills_collection"].insert_one(encrypt_dict(doc, BILL_SENSITIVE))
    return str(res.inserted_id)


async def _read_bill(mocked_db, bill_id: str) -> dict:
    raw = await mocked_db["bills_collection"].find_one({"_id": ObjectId(bill_id)})
    return decrypt_dict(raw, BILL_SENSITIVE)


def _gp_item(**kw) -> dict:
    base = {"item_name": "Pillow", "specification": None, "category": "Bed", "received_qty": 0}
    base.update(kw)
    return base


def _bill(*, gp_id="gp-x", qty=10, unit_price=100, paid=0.0, status="PENDING", **extra) -> dict:
    total = round(unit_price * qty, 2)
    doc = {
        "gate_pass_id": gp_id,
        "client_name": "Test Client",
        "items": [
            {"item_name": "Pillow", "category": "Bed", "unit_price": unit_price,
             "quantity": qty, "line_total": total}
        ],
        "total_amount": total,
        "total_quantity": float(qty),
        "grand_total": total,
        "outstanding_amount": round(total - paid, 2),
        "discounts": 0,
        "transport_fee": 0,
        "taxes": 0,
        "additional_charges": 0,
        "paid_amount": paid,
        "notes": "",
        "payment_status": status,
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc.update(extra)
    return doc


def _user(name="Alice"):
    return {"auth_id": f"u-{name}", "user_name": name, "role": "ADMIN"}


class _Req:
    def __init__(self, key=None):
        self.headers = {"X-Idempotency-Key": key} if key else {}


# ── derive_payment_status ────────────────────────────────────────────────────


def test_status_follows_the_money():
    f = bill_sync.derive_payment_status
    assert f(1000, 0, "PENDING") == "PENDING"
    assert f(1000, 400, "PENDING") == "PARTIALLY_PAID"
    assert f(1000, 1000, "PARTIALLY_PAID") == "PAID"
    # A correction that takes the total below what was already paid settles it.
    assert f(400, 1000, "PARTIALLY_PAID") == "PAID"
    # Pre-issue / pre-draft states are workflow labels, not money labels.
    assert f(1000, 0, "DRAFT") == "DRAFT"
    assert f(1000, 0, "ISSUED") == "ISSUED"
    assert f(1000, 0, "CANCELLED") == "CANCELLED"


# ── The re-sync must move the status with the total ──────────────────────────


async def test_a_correction_that_settles_the_bill_marks_it_paid(mocked_db):
    """10 x 100 partly paid, corrected down to 4 pieces: 0 owed, so PAID.

    Left as PARTIALLY_PAID with an outstanding of 0 the bill sat in the
    outstanding list forever with nothing to collect.
    """
    bill_id = await _store_bill(
        mocked_db, _bill(gp_id="gp-settle", qty=10, paid=600.0, status="PARTIALLY_PAID")
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        "gp-settle", [_gp_item(received_qty=4)], user_id="u-1"
    )

    assert outcomes[0]["action"] == "adjusted"
    after = await _read_bill(mocked_db, bill_id)
    assert after["items"][0]["quantity"] == 4
    assert after["grand_total"] == 400.0
    assert after["outstanding_amount"] == 0.0
    assert after["payment_status"] == "PAID"


async def test_a_correction_that_leaves_a_gap_keeps_it_part_paid(mocked_db):
    bill_id = await _store_bill(
        mocked_db, _bill(gp_id="gp-gap", qty=10, paid=200.0, status="PARTIALLY_PAID")
    )
    await bill_sync.sync_bills_to_gate_pass("gp-gap", [_gp_item(received_qty=5)], user_id="u-1")
    after = await _read_bill(mocked_db, bill_id)
    assert after["grand_total"] == 500.0
    assert after["outstanding_amount"] == 300.0
    assert after["payment_status"] == "PARTIALLY_PAID"


async def test_an_unpaid_bill_corrected_down_stays_pending(mocked_db):
    bill_id = await _store_bill(mocked_db, _bill(gp_id="gp-unpaid", qty=10, status="PENDING"))
    await bill_sync.sync_bills_to_gate_pass("gp-unpaid", [_gp_item(received_qty=6)], user_id="u-1")
    after = await _read_bill(mocked_db, bill_id)
    assert after["outstanding_amount"] == 600.0
    assert after["payment_status"] == "PENDING"


async def test_a_paid_bill_is_flagged_and_never_rewritten(mocked_db):
    bill_id = await _store_bill(
        mocked_db, _bill(gp_id="gp-paid", qty=10, paid=1000.0, status="PAID")
    )
    outcomes = await bill_sync.sync_bills_to_gate_pass(
        "gp-paid", [_gp_item(received_qty=2)], user_id="u-1"
    )
    assert outcomes[0]["action"] == "flagged"
    after = await _read_bill(mocked_db, bill_id)
    assert after["items"][0]["quantity"] == 10
    assert after["payment_status"] == "PAID"
    assert "NOT re-written" in after["notes"]


# ── Manual bill edit: same rule ─────────────────────────────────────────────


async def test_editing_a_part_paid_bill_down_to_nothing_marks_it_paid(mocked_db):
    from bill_service.routers.bills import BillEdit

    bill_id = await _store_bill(
        mocked_db, _bill(gp_id="gp-edit", qty=10, paid=500.0, status="PARTIALLY_PAID")
    )
    await bills_router.edit_bill(
        bill_id,
        BillEdit(
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 2}]
        ),
        _user(),
    )
    after = await _read_bill(mocked_db, bill_id)
    assert after["grand_total"] == 200.0
    assert after["outstanding_amount"] == 0.0
    assert after["payment_status"] == "PAID"


# ── Payment cap is read from the money, not a cached figure ──────────────────


async def test_a_payment_cannot_exceed_a_stale_cached_outstanding(mocked_db):
    """`outstanding_amount` was the cap, and a correction can leave it behind.

    Here the cached figure says 1000 is owed but only 400 actually is. Paying
    1000 would have recorded a 600 overpayment that nothing could ever settle.
    """
    from bill_service.models import PaymentCreate

    bill_id = await _store_bill(
        mocked_db,
        _bill(
            gp_id="gp-stale",
            qty=10,
            paid=600.0,
            status="PARTIALLY_PAID",
            # A stale cache left over from before the correction.
            outstanding_amount=1000.0,
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await bills_router.create_payment(
            bill_id,
            PaymentCreate(amount=1000.0, payment_method="CASH", payment_date=_now()),
            _user(),
            _Req(),
        )
    assert exc.value.status_code == 400
    assert "exceeds outstanding block of 400.0" in exc.value.detail

    # Nothing was written, so no orphan payment exists.
    assert await mocked_db["payments_collection"].count_documents({}) == 0
    after = await _read_bill(mocked_db, bill_id)
    assert after["paid_amount"] == 600.0


async def test_a_payment_within_the_real_outstanding_is_accepted(mocked_db):
    from bill_service.models import PaymentCreate

    bill_id = await _store_bill(
        mocked_db,
        _bill(gp_id="gp-ok", qty=10, paid=600.0, status="PARTIALLY_PAID", outstanding_amount=1000.0),
    )
    await bills_router.create_payment(
        bill_id,
        PaymentCreate(amount=400.0, payment_method="CASH", payment_date=_now()),
        _user(),
        _Req(),
    )
    after = await _read_bill(mocked_db, bill_id)
    assert after["paid_amount"] == 1000.0
    assert after["outstanding_amount"] == 0.0
    assert after["payment_status"] == "PAID"
    assert await mocked_db["payments_collection"].count_documents({}) == 1


async def test_a_retried_payment_is_not_recorded_twice(mocked_db):
    from bill_service.models import PaymentCreate

    bill_id = await _store_bill(mocked_db, _bill(gp_id="gp-retry", qty=4))
    payload = PaymentCreate(amount=100.0, payment_method="CASH", payment_date=_now())
    await bills_router.create_payment(bill_id, payload, _user(), _Req("pay-1"))
    await bills_router.create_payment(bill_id, payload, _user(), _Req("pay-1"))
    assert await mocked_db["payments_collection"].count_documents({}) == 1
    after = await _read_bill(mocked_db, bill_id)
    assert after["paid_amount"] == 100.0


async def test_two_identical_payments_are_two_payments(mocked_db):
    """A body-hash key made the second identical payment vanish."""
    from bill_service.models import PaymentCreate

    bill_id = await _store_bill(mocked_db, _bill(gp_id="gp-two", qty=10))
    payload = PaymentCreate(amount=100.0, payment_method="CASH", payment_date=_now())
    await bills_router.create_payment(bill_id, payload, _user(), _Req("a"))
    await bills_router.create_payment(bill_id, payload, _user(), _Req("b"))
    assert await mocked_db["payments_collection"].count_documents({}) == 2
    after = await _read_bill(mocked_db, bill_id)
    assert after["paid_amount"] == 200.0
    assert after["payment_status"] == "PARTIALLY_PAID"

"""Day money: income/collected/outstanding picture for one business day.

Read-only view over the gate-pass bills and their payment records so the
Daily Operations screen (and the Close Day snapshot) can show the money side
of a day without cross-querying the whole collection. Money fields are stored
plain-text on the documents (only client/notes/items are encrypted), so a
projection is safe here.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import bills_collection, payments_collection

router = APIRouter(prefix="/day-money", tags=["day-money"])

UNPAID_STATUSES = ["PENDING", "PARTIALLY_PAID", "OVERDUE"]


def _validate_day(date: str) -> datetime:
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
        return day.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")


@router.get("")
async def day_money(
    date: str = Query(..., description="Business day YYYY-MM-DD"),
    current_user: dict = Depends(require_capability("bill:read")),
):
    """Snapshot of the money recorded for the given calendar day."""
    start = _validate_day(date)
    end = start + timedelta(days=1)

    bills_created = 0
    billed_amount = 0.0
    billed_paid = 0.0
    bill_cursor = bills_collection.find(
        {"created_at": {"$gte": start, "$lt": end}, "payment_status": {"$ne": "CANCELLED"}},
        {"grand_total": 1, "paid_amount": 1, "payment_status": 1},
    )
    async for bill in bill_cursor:
        bills_created += 1
        billed_amount += float(bill.get("grand_total", 0) or 0)
        billed_paid += float(bill.get("paid_amount", 0) or 0)

    payments_count = 0
    collected_amount = 0.0
    pay_cursor = payments_collection.find(
        {"payment_date": {"$gte": start, "$lt": end}},
        {"amount": 1, "payment_date": 1},
    )
    async for pay in pay_cursor:
        payments_count += 1
        collected_amount += float(pay.get("amount", 0) or 0)

    open_bills_count = 0
    outstanding_amount = 0.0
    agg_cursor = bills_collection.aggregate(
        [
            {"$match": {"payment_status": {"$in": UNPAID_STATUSES}}},
            {"$group": {"_id": None, "count": {"$sum": 1}, "total": {"$sum": "$outstanding_amount"}}},
        ]
    )
    async for row in agg_cursor:
        open_bills_count = int(row.get("count", 0) or 0)
        outstanding_amount = float(row.get("total", 0) or 0)

    return {
        "date": date,
        "bills_created": bills_created,
        "billed_amount": round(billed_amount, 2),
        "billed_paid_amount": round(billed_paid, 2),
        "payments_count": payments_count,
        "collected_amount": round(collected_amount, 2),
        "open_bills_count": open_bills_count,
        "outstanding_amount": round(outstanding_amount, 2),
    }
"""Day close: append-only end-of-day snapshot in the event journal.

Closing a business day records a ``DAY_CLOSED`` journal entry carrying an
immutable snapshot of that day's operational totals (gate passes, pieces,
deliveries, outstanding pieces, open adjustments, reconciliation issues).
It never locks the day and never mutates any document — it is a signed-off,
point-in-time picture of the day.

The event is timestamped at the end of the business day (UTC) so it appears
at the end of that day's timeline; the real wall-clock close time is kept in
``meta.closed_at``.
"""
from datetime import datetime, date, time, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel

from ..auth_helper import require_capability
from ..database.main_db import linen_events_collection
from ..services.transaction_events import EVENT_DAY_CLOSED, record_event

router = APIRouter(prefix="/day-close", tags=["day-close"])


class DayCloseRequest(BaseModel):
    date: str
    totals: Dict[str, Any]
    note: Optional[str] = None


def _parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")


def ensure_not_future(day: date, today: date) -> None:
    """Guardrail: never close a business day that is ahead of "today".

    ``today`` is compared with one day of headroom because the service runs
    on UTC while the caller operates on a local calendar day (e.g. +5:30).
    A genuinely future date (next week) is always rejected.
    """
    if day > today + timedelta(days=1):
        raise HTTPException(
            status_code=409, detail=f"Cannot close a future day ({day.isoformat()})."
        )


def _validate_date(value: str) -> datetime:
    return datetime.combine(_parse_day(value), time(hour=23, minute=59, second=59, tzinfo=timezone.utc))


@router.get("")
async def get_day_close(
    date: str = Query(..., description="Business day YYYY-MM-DD"),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """Return the latest DAY_CLOSED snapshot for a business day, or null."""
    _validate_date(date)
    doc = await linen_events_collection.find_one(
        {"entity_type": "day", "entity_id": date, "event_type": EVENT_DAY_CLOSED},
        sort=[("occurred_at", -1)],
    )
    if not doc:
        return None
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.post("")
async def close_day(
    payload: DayCloseRequest,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Record an end-of-day snapshot. Append-only; safe to run repeatedly."""
    day_end = _validate_date(payload.date)
    ensure_not_future(day_end.date(), datetime.now(timezone.utc).date())

    # Guardrail: never close a day out of order. If a later day already has a
    # snapshot, closing this one would silently imply an un-closed gap exists.
    out_of_order = await linen_events_collection.find_one(
        {
            "entity_type": "day",
            "event_type": EVENT_DAY_CLOSED,
            "occurred_at": {"$gt": day_end},
        },
        sort=[("occurred_at", 1)],
    )
    if out_of_order:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A later day ({out_of_order.get('entity_id')}) is already closed. "
                "Close days in order, or re-close the later day first."
            ),
        )

    now = datetime.now(timezone.utc)

    event_id = await record_event(
        entity_type="day",
        entity_id=payload.date,
        event_type=EVENT_DAY_CLOSED,
        user_id=current_user.get("auth_id", ""),
        user_name=current_user.get("user_name", ""),
        reason=payload.note or None,
        meta={"totals": payload.totals, "closed_at": now.isoformat()},
        occurred_at=day_end,
    )
    doc = await linen_events_collection.find_one({"_id": event_id})
    if doc:
        doc["id"] = str(doc.pop("_id"))
    return doc
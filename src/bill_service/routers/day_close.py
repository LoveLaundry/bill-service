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
from datetime import datetime, time, timezone
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


def _validate_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")


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
    _validate_date(payload.date)

    now = datetime.now(timezone.utc)
    day_end = datetime.combine(
        _validate_date(payload.date).date(),
        time(hour=23, minute=59, second=59, tzinfo=timezone.utc),
    )

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
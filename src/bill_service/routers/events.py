"""Event journal timeline (append-only read).

Powers the "Daily Timeline" on the Daily Operations screen and the per
transaction timelines. Read-only view over ``linen_events``.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..app_time import day_bounds, day_end, day_start
from ..auth_helper import require_capability
from ..database.main_db import linen_events_collection

router = APIRouter(prefix="/events", tags=["events"])


@router.get("")
async def list_events(
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    gate_pass_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    date: Optional[datetime] = Query(None, description="ISO date; matches events on that Sri Lankan calendar day"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    query: dict = {}

    if entity_type:
        query["entity_type"] = entity_type
    if entity_id:
        query["entity_id"] = entity_id
    if gate_pass_id:
        query["gate_pass_id"] = gate_pass_id
    if event_type:
        query["event_type"] = event_type

    date_range: dict = {}
    if date:
        # One Sri Lankan business day, not a UTC one.
        start, end = day_bounds(date)
        date_range["$gte"] = start
        date_range["$lt"] = end
    if date_from:
        date_range["$gte"] = day_start(date_from)
    if date_to:
        date_range["$lte"] = day_end(date_to)
    if date_range:
        query["occurred_at"] = date_range

    cursor = (
        linen_events_collection.find(query)
        .sort("occurred_at", -1)
        .skip(skip)
        .limit(limit)
    )
    results = []
    async for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        results.append(doc)
    return results
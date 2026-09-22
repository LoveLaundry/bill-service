"""Append-only transaction/event journal.

Every physical or financial quantity change on a gate pass, delivery,
return, adjustment, or bill writes an immutable journal entry into the
``linen_events`` collection. The journal makes it possible to reconstruct
exactly what happened: event type, quantity affected, user, timestamp,
related transaction, reason, and the previous/new status.

Notes are NOT part of this journal. A note is only ever carried inside
``meta`` alongside a real quantity-bearing event.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from ..database.main_db import linen_events_collection

# Event type constants
EVENT_GATE_PASS_CREATED = "GATE_PASS_CREATED"
EVENT_STATUS_CHANGED = "STATUS_CHANGED"
EVENT_RECEIVING_EDITED = "RECEIVING_EDITED"
EVENT_RECEIVING_DATE_CHANGED = "RECEIVING_DATE_CHANGED"
EVENT_DELIVERY_CREATED = "DELIVERY_CREATED"
EVENT_RETURN_CREATED = "RETURN_CREATED"
EVENT_RETURN_UPDATED = "RETURN_UPDATED"
EVENT_RETURN_RESENT = "RETURN_RESENT"
EVENT_ADJUSTMENT_REQUESTED = "ADJUSTMENT_REQUESTED"
EVENT_ADJUSTMENT_APPROVED = "ADJUSTMENT_APPROVED"
EVENT_ADJUSTMENT_REJECTED = "ADJUSTMENT_REJECTED"
EVENT_CATCH_UP_DELIVERY = "CATCH_UP_DELIVERY"
EVENT_LEGACY_FLAG = "LEGACY_FLAG"
EVENT_BILL_CREATED = "BILL_CREATED"
EVENT_DAY_CLOSED = "DAY_CLOSED"
# Historical, non-quantity closure. Kept for migration visibility only;
# new closures MUST go through EVENT_CATCH_UP_DELIVERY with real quantities.
EVENT_LEGACY_NOTE_CLOSURE = "LEGACY_NOTE_CLOSURE"


async def record_event(
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    gate_pass_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    item_deltas: Optional[list] = None,
    reason: Optional[str] = None,
    meta: Optional[dict] = None,
    prev_status: Optional[str] = None,
    new_status: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
):
    """Append a journal entry. Never mutates any existing document.

    ``item_deltas`` is a list of ``{"item_name", "specification",
    "qty_before", "qty_after", "qty_delta"}`` records.
    """
    event_doc: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "event_type": event_type,
        "occurred_at": occurred_at or datetime.now(timezone.utc),
    }
    if gate_pass_id:
        event_doc["gate_pass_id"] = gate_pass_id
    if user_id:
        event_doc["user_id"] = user_id
    if user_name:
        event_doc["user_name"] = user_name
    if item_deltas:
        event_doc["item_deltas"] = item_deltas
    if reason:
        event_doc["reason"] = reason
    if meta:
        event_doc["meta"] = meta
    if prev_status:
        event_doc["prev_status"] = prev_status
    if new_status:
        event_doc["new_status"] = new_status

    result = await linen_events_collection.insert_one(event_doc)
    return result.inserted_id


def build_item_delta(
    item_name: str,
    specification: Optional[str],
    qty_before: int,
    qty_after: int,
) -> dict:
    """Build a single item-delta record for a journal entry."""
    return {
        "item_name": item_name,
        "specification": specification or "",
        "qty_before": qty_before,
        "qty_after": qty_after,
        "qty_delta": qty_after - qty_before,
    }
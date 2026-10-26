"""Per-laundry live clock for instant UI refresh.

A QR scan or linen field change increments a monotonic counter for that
laundry. Other sessions poll the counter and refetch as soon as it moves,
so inventory/dashboard/scanner screens do not wait on cache staleTime.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .database.main_db import live_clocks_collection


async def read_live_clock(laundry_id: str) -> int:
    doc = await live_clocks_collection.find_one({"laundry_id": laundry_id})
    if not doc:
        return 0
    return int(doc.get("clock") or 0)


async def bump_live_clock(laundry_id: str, reason: str = "update") -> int:
    now = datetime.now(timezone.utc)
    result = await live_clocks_collection.find_one_and_update(
        {"laundry_id": laundry_id},
        {"$inc": {"clock": 1}, "$set": {"updated_at": now, "reason": reason}},
        upsert=True,
        return_document=True,
    )
    if result is None:
        doc = await live_clocks_collection.find_one({"laundry_id": laundry_id})
        return int(doc.get("clock") or 1) if doc else 1
    return int(result.get("clock") or 1)

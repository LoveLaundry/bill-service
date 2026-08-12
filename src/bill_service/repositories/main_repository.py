"""
MAIN repository - version-aware write/read helpers for the production
source-of-truth database.

The repository appends a monotonic `sync_version` bump and records the
change in the durable `sync_queue` so the background worker can replicate
it to the SECONDARY database.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..database.main_db import sync_queue_collection, sync_status_collection
from .entity_registry import get_main_collection


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def bump_version(entity: str, record_id: Any, reason: str = "update") -> int:
    """
    Atomically increment the `sync_version` of a MAIN document.

    Returns the new version. The document is updated via $inc so legacy
    documents (no field present) start at version 1.
    """
    collection = get_main_collection(entity)
    result = await collection.find_one_and_update(
        {"_id": record_id},
        {"$inc": {"sync_version": 1}, "$set": {"updated_at": _now()}},
        return_document=True,
    )
    if result is None:
        raise KeyError(f"{entity} {record_id} not found in MAIN database")
    return int(result.get("sync_version") or 1)


async def enqueue_sync(entity: str, record_id: Any, version: int) -> None:
    """Persist a sync job in the durable MAIN sync_queue (retry-safe)."""
    await sync_queue_collection.update_one(
        {"entity": entity, "record_id": str(record_id), "status": {"$in": ["PENDING", "FAILED"]}},
        {
            "$set": {
                "entity": entity,
                "record_id": str(record_id),
                "version": version,
                "status": "PENDING",
                "attempts": 0,
                "next_attempt_at": _now(),
                "updated_at": _now(),
            },
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )


async def record_sync_status(
    entity: str,
    record_id: Any,
    main_version: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Upsert the sync_status row for one record. Idempotent by design."""
    await sync_status_collection.update_one(
        {"entity": entity, "record_id": str(record_id)},
        {
            "$set": {
                "entity": entity,
                "record_id": str(record_id),
                "main_version": main_version,
                "status": status,
                "error": error,
                "checked_at": _now(),
            },
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )


async def get_sync_status(entity: str, record_id: Any) -> Optional[dict]:
    """Fetch the sync metadata row for a record (or None)."""
    return await sync_status_collection.find_one(
        {"entity": entity, "record_id": str(record_id)}
    )


async def attach_verification(entity: str, record_id: Any, serialized: dict) -> dict:
    """
    Enrich an already-serialized business response with its verification
    state, read from MAIN sync_status.
    """
    row = await get_sync_status(entity, record_id)
    if not row:
        serialized["verification"] = {
            "status": "PENDING",
            "verified": False,
            "last_verified_at": None,
        }
        return serialized

    status = row.get("status", "PENDING")
    serialized["verification"] = {
        "status": status,
        "verified": status == "VERIFIED",
        "last_verified_at": row.get("last_verified_at"),
        "main_version": row.get("main_version"),
        "secondary_version": row.get("secondary_version"),
        "error": row.get("error"),
    }
    return serialized

"""
Verification service - compares MAIN and SECONDARY versions and records
the outcome in the MAIN sync_status collection.

State machine:
    PENDING   -> change made in MAIN, not yet synced/verified
    SYNCING   -> a worker is currently replicating the record
    VERIFIED  -> MAIN version == SECONDARY version
    FAILED    -> replication failed after retries
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..database.main_db import sync_status_collection
from ..repositories.main_repository import get_sync_status, record_sync_status
from ..repositories.secondary_repository import verify_document

STATUS_PENDING = "PENDING"
STATUS_SYNCING = "SYNCING"
STATUS_VERIFIED = "VERIFIED"
STATUS_FAILED = "FAILED"

STATUSES = (STATUS_PENDING, STATUS_SYNCING, STATUS_VERIFIED, STATUS_FAILED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def mark_syncing(entity: str, record_id: Any, main_version: int) -> None:
    await record_sync_status(entity, record_id, main_version, STATUS_SYNCING)


async def mark_verified(entity: str, record_id: Any, main_version: int) -> None:
    await sync_status_collection.update_one(
        {"entity": entity, "record_id": str(record_id)},
        {
            "$set": {
                "entity": entity,
                "record_id": str(record_id),
                "main_version": main_version,
                "secondary_version": main_version,
                "status": STATUS_VERIFIED,
                "error": None,
                "last_verified_at": _now(),
                "checked_at": _now(),
            },
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )


async def mark_failed(entity: str, record_id: Any, main_version: int, error: str) -> None:
    await record_sync_status(entity, record_id, main_version, STATUS_FAILED, error=error)


async def verify_against_secondary(entity: str, record_id: Any, main_version: int) -> str:
    """
    Read the record from SECONDARY, compare versions, and update MAIN status.

    Returns the resulting status: VERIFIED, PENDING (mismatch) or FAILED.
    """
    try:
        matched = await verify_document(entity, record_id, main_version, _now())
    except Exception as exc:
        await mark_failed(entity, record_id, main_version, str(exc))
        return STATUS_FAILED

    if matched:
        await mark_verified(entity, record_id, main_version)
        return STATUS_VERIFIED

    await record_sync_status(entity, record_id, main_version, STATUS_PENDING)
    return STATUS_PENDING


async def get_verification(entity: str, record_id: Any) -> dict:
    """Return the public verification payload for a record."""
    row = await get_sync_status(entity, record_id)
    if not row:
        return {
            "status": STATUS_PENDING,
            "verified": False,
            "last_verified_at": None,
        }

    status = row.get("status", STATUS_PENDING)
    return {
        "status": status,
        "verified": status == STATUS_VERIFIED,
        "last_verified_at": row.get("last_verified_at"),
        "main_version": row.get("main_version"),
        "secondary_version": row.get("secondary_version"),
        "error": row.get("error"),
    }


async def attach_verification_to(entity: str, record_id: Any, serialized: dict) -> dict:
    """Attach verification info to a serialized business document."""
    serialized["verification"] = await get_verification(entity, record_id)
    return serialized

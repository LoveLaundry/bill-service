"""
SECONDARY repository - verification/replica access.

This repository is consumed ONLY by the synchronization service and the
verification service. Business routers must never import it.
"""
from __future__ import annotations

from typing import Any, Optional

from ..database.secondary_db import sync_status_collection
from .entity_registry import effective_version, get_secondary_collection


async def fetch_document(entity: str, record_id: Any) -> Optional[dict]:
    """Read the raw (encrypted) document from the SECONDARY database."""
    collection = get_secondary_collection(entity)
    return await collection.find_one({"_id": record_id})


async def upsert_document(entity: str, document: dict) -> None:
    """
    Write a raw MAIN document (including its `sync_version` and encrypted
    payload) into the SECONDARY database. Preserves the original _id.
    """
    collection = get_secondary_collection(entity)
    record_id = document["_id"]
    await collection.replace_one(
        {"_id": record_id}, document, upsert=True
    )


async def update_sync_status(
    entity: str,
    record_id: Any,
    main_version: int,
    secondary_version: int,
    status: str,
    last_verified_at: Any = None,
    error: Optional[str] = None,
) -> None:
    """Persist version comparison state inside the SECONDARY database."""
    await sync_status_collection.update_one(
        {"entity": entity, "record_id": str(record_id)},
        {
            "$set": {
                "entity": entity,
                "record_id": str(record_id),
                "main_version": main_version,
                "secondary_version": secondary_version,
                "status": status,
                "error": error,
            },
            "$setOnInsert": {"created_at": last_verified_at},
        },
        upsert=True,
    )


async def verify_document(entity: str, record_id: Any, main_version: int, last_verified_at: Any) -> bool:
    """
    Read the record in SECONDARY and compare sync_version with the MAIN
    version. Returns True when they match.
    """
    doc = await fetch_document(entity, record_id)
    if not doc:
        return False
    secondary_version = effective_version(doc)
    matched = secondary_version == main_version
    await update_sync_status(
        entity=entity,
        record_id=record_id,
        main_version=main_version,
        secondary_version=secondary_version,
        status="VERIFIED" if matched else "PENDING",
        last_verified_at=last_verified_at if matched else None,
        error=None if matched else "Version mismatch: main and secondary differ",
    )
    return matched

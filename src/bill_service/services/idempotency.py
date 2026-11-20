"""Idempotency guard for create endpoints.

A client that may retry a POST (network drop, offline queue) sends an
``X-Idempotency-Key`` header. The FIRST request creates the record and
memorizes key -> entity_id. Any retry with the same key returns the
already-created entity instead of creating a duplicate.

Keys are namespaced by user, laundry, entity type, and request path so retries
cannot replay another tenant's or endpoint's record.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request
from bson import ObjectId

from ..database.main_db import idempotency_collection
from ..tenancy import LOVE_LAUNDRY_ID


def _clean_key(request: Request) -> Optional[str]:
    key = request.headers.get("X-Idempotency-Key")
    if not key:
        return None
    cleaned = (key or "").strip()
    if not cleaned or len(cleaned) > 128:
        raise HTTPException(status_code=400, detail="Invalid X-Idempotency-Key header.")
    return cleaned


def _get_key(request: Request, user_id: str) -> Optional[str]:
    cleaned = _clean_key(request)
    return f"{user_id}:{cleaned}" if cleaned else None


def _scoped_key(
    request: Request, user_id: str, laundry_id: str, entity_type: str
) -> Optional[str]:
    cleaned = _clean_key(request)
    if not cleaned:
        return None
    path = getattr(getattr(request, "url", None), "path", "")
    return f"{user_id}:{laundry_id}:{entity_type}:{path}:{cleaned}"


async def find_previous(
    request: Request,
    user_id: str,
    laundry_id: str,
    entity_type: str,
    entity_collection,
) -> object:
    """Return the previously created doc for this key, or None.

    ``entity_collection`` is any AsyncIOMotor collection whose docs have
    ObjectId ``_id`` values (raw stored doc, still encrypted as stored).
    """
    key = _scoped_key(request, user_id, laundry_id, entity_type)
    if not key:
        return None
    entry = await idempotency_collection.find_one({"key": key})
    if not entry:
        # Continue honoring records written before tenant/entity namespacing.
        legacy_key = _get_key(request, user_id)
        if legacy_key:
            entry = await idempotency_collection.find_one(
                {"key": legacy_key, "entity_type": entity_type}
            )
    if not entry:
        return None
    if entry.get("entity_type") != entity_type:
        return None
    entity_id = entry.get("entity_id")
    try:
        oid = ObjectId(entity_id)
    except Exception:
        return None
    doc = await entity_collection.find_one({"_id": oid})
    if not doc:
        return None
    doc_laundry_id = doc.get("laundry_id") or LOVE_LAUNDRY_ID
    return doc if doc_laundry_id == laundry_id else None


async def record_created(
    request: Request,
    user_id: str,
    entity_type: str,
    entity_id: str,
    laundry_id: str,
) -> None:
    """Memorize key -> entity after a successful create."""
    key = _scoped_key(request, user_id, laundry_id, entity_type)
    if not key:
        return
    try:
        await idempotency_collection.insert_one(
            {
                "key": key,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception:
        # Duplicate key insert (concurrent retry already recorded) — fine.
        pass


async def clear(
    request: Request, user_id: str, laundry_id: str, entity_type: str
) -> None:
    """Forget a key whose create was rolled back.

    Without this, a request that lost a concurrency check would burn its
    idempotency key and every later retry would resolve to nothing.
    """
    key = _scoped_key(request, user_id, laundry_id, entity_type)
    if not key:
        return
    await idempotency_collection.delete_one({"key": key})
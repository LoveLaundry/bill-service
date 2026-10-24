"""
Linen Tracking — CRUD, scanning, events, stats, tag generation.
"""
import random
import re
import string
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import get_current_user, require_capability
from ..tenancy import assert_same_laundry, owned_filter, resolve_laundry_id, scoped_query
from ..crypto_helper import get_search_token
from ..database.main_db import (
    linens_collection,
    linen_events_collection,
    audit_collection,
    bills_collection,
)
from ..repositories.main_repository import bump_version, enqueue_delete, enqueue_sync
from ..models import (
    LinenCreate,
    LinenBulkCreate,
    LinenUpdate,
    LinenScanAction,
    LinenModel,
    LinenEventModel,
    LinenListResponse,
    LinenEventListResponse,
    LinenStats,
    LinenTagGenerate,
    LINEN_STATUSES,
)

router = APIRouter(prefix="/linens", tags=["linens"])

# User-supplied search terms are substring lookups, never expressions. Interpolating
# them into `$regex` directly allowed callers to inject their own pattern (`.*`
# returned the whole collection) and to trigger catastrophic backtracking, which
# pins the Mongo matcher. Escape the metacharacters and cap the length instead.
MAX_SEARCH_LENGTH = 100


def _search_pattern(term: Optional[str]) -> str:
    """Return a literal, length-bounded Mongo `$regex` body."""
    if term is None:
        return ""
    return re.escape(str(term)[:MAX_SEARCH_LENGTH])

# --- Linen ID generator ---
# Safe charset: avoids O/0/I/1/l to prevent confusion
SAFE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def generate_linen_id() -> str:
    """Generate a unique linen ID like LL-7K4P92."""
    suffix = "".join(random.choices(SAFE_CHARS, k=6))
    return f"LL-{suffix}"


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc["_id"])
    del doc["_id"]
    return doc


def _parse_object_id(linen_id: str) -> ObjectId:
    try:
        return ObjectId(linen_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid linen document ID")


async def _owned_linen(oid, current_user: dict, label: str = "Linen not found"):
    """Fetch a linen doc iff it belongs to the caller's laundry."""
    laundry_id = resolve_laundry_id(current_user)
    doc = await linens_collection.find_one({"_id": oid})
    assert_same_laundry(doc, laundry_id, "Linen")
    if not doc:
        raise HTTPException(status_code=404, detail=label)
    return laundry_id, doc


async def _log_audit(user_id: str, action: str, entity: str, entity_id: str, laundry_id: str | None = None):
    # One writer, one schema — see router_utils.log_audit.
    from ..router_utils import log_audit as canonical_log_audit
    return await canonical_log_audit(user_id, action, entity, entity_id, None, None, laundry_id)


# ============================================================
#  CREATE — single linen
# ============================================================
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_linen(
    payload: LinenCreate,
    current_user: dict = Depends(require_capability("linen:write")),
):
    now = datetime.now(timezone.utc)
    laundry_id = resolve_laundry_id(current_user)
    linen_id = generate_linen_id()

    # Ensure uniqueness (retry on collision, max 5 attempts)
    for _ in range(5):
        existing = await linens_collection.find_one({"linen_id": linen_id})
        if not existing:
            break
        linen_id = generate_linen_id()

    doc = {
        "laundry_id": laundry_id,
        "linen_id": linen_id,
        "category": payload.category,
        "item_type": payload.item_type,
        "description": payload.description,
        "size": payload.size,
        "color": payload.color,
        "client_name": payload.client_name,
        "client_name_search": get_search_token(payload.client_name),
        "department": payload.department,
        "status": "IN_STOCK",
        "condition": "NEW",
        "location": None,
        "wash_count": 0,
        "last_washed_date": None,
        "last_scanned_date": None,
        "retirement_date": None,
        "notes": payload.notes,
        "created_at": now,
        "updated_at": now,
    }

    result = await linens_collection.insert_one(doc)
    doc["_id"] = result.inserted_id

    await _log_audit(current_user.get("user_id", ""), "create", "linen", str(result.inserted_id))

    return _serialize(doc)


# ============================================================
#  CREATE — bulk linens
# ============================================================
@router.post("/bulk", status_code=status.HTTP_201_CREATED)
async def create_linen_bulk(
    payload: LinenBulkCreate,
    current_user: dict = Depends(require_capability("linen:write")),
):
    now = datetime.now(timezone.utc)
    laundry_id = resolve_laundry_id(current_user)
    created = []
    linen_ids = set()

    for _ in range(payload.quantity):
        # Generate unique ID
        for _attempt in range(10):
            lid = generate_linen_id()
            if lid not in linen_ids:
                existing = await linens_collection.find_one({"linen_id": lid})
                if not existing:
                    linen_ids.add(lid)
                    break
        else:
            raise HTTPException(status_code=500, detail="Failed to generate unique linen ID after 10 attempts")

        doc = {
            "laundry_id": laundry_id,
            "linen_id": lid,
            "category": payload.category,
            "item_type": payload.item_type,
            "description": payload.description,
            "size": payload.size,
            "color": payload.color,
            "client_name": payload.client_name,
            "department": payload.department,
            "status": "IN_STOCK",
            "condition": "NEW",
            "location": None,
            "wash_count": 0,
            "last_washed_date": None,
            "last_scanned_date": None,
            "retirement_date": None,
            "notes": payload.notes,
            "created_at": now,
            "updated_at": now,
        }
        result = await linens_collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        created.append(_serialize(doc))

    await _log_audit(current_user.get("user_id", ""), "bulk_create", "linen", f"count={len(created)}")

    return {"items": created, "count": len(created)}


# ============================================================
#  LIST linens
# ============================================================
@router.get("", response_model=LinenListResponse)
async def list_linens(
    search: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    client_name: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=5000),
    current_user: dict = Depends(require_capability("linen:read")),
):
    query: dict = {}
    if search:
        pattern = _search_pattern(search)
        query["$or"] = [
            {"linen_id": {"$regex": pattern, "$options": "i"}},
            {"item_type": {"$regex": pattern, "$options": "i"}},
            {"client_name": {"$regex": pattern, "$options": "i"}},
            {"description": {"$regex": pattern, "$options": "i"}},
        ]
    if category:
        query["category"] = category
    if status_filter:
        query["status"] = status_filter
    if client_name:
        query["client_name"] = {"$regex": _search_pattern(client_name), "$options": "i"}
    if condition:
        query["condition"] = condition

    sort_dir = 1 if sort_order == "asc" else -1
    valid_sorts = {"created_at", "linen_id", "category", "status", "client_name", "wash_count", "last_scanned_date", "updated_at"}
    sort_field = sort_by if sort_by in valid_sorts else "created_at"

    query = scoped_query(query, resolve_laundry_id(current_user))
    total = await linens_collection.count_documents(query)
    cursor = linens_collection.find(query).sort(sort_field, sort_dir).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        items.append(_serialize(doc))

    return LinenListResponse(items=items, total=total)


# ============================================================
#  GET single linen
# ============================================================
@router.get("/{doc_id}")
async def get_linen(
    doc_id: str,
    current_user: dict = Depends(require_capability("linen:read")),
):
    oid = _parse_object_id(doc_id)
    _, doc = await _owned_linen(oid, current_user)
    return _serialize(doc)


# ============================================================
#  GET linen by linen_id (the LL-XXXXXX code)
# ============================================================
@router.get("/by-code/{linen_code}")
async def get_linen_by_code(
    linen_code: str,
    current_user: dict = Depends(require_capability("linen:read")),
):
    laundry_id = resolve_laundry_id(current_user)
    doc = await linens_collection.find_one({"linen_id": linen_code.upper()})
    assert_same_laundry(doc, laundry_id, "Linen")
    if not doc:
        raise HTTPException(status_code=404, detail="Linen not found")
    # Update last_scanned_date
    now = datetime.now(timezone.utc)
    await linens_collection.update_one(
        owned_filter(doc["_id"], laundry_id),
        {"$set": {"last_scanned_date": now, "updated_at": now}},
    )
    doc["last_scanned_date"] = now
    return _serialize(doc)


# ============================================================
#  UPDATE linen
# ============================================================
@router.patch("/{doc_id}")
async def update_linen(
    doc_id: str,
    payload: LinenUpdate,
    current_user: dict = Depends(require_capability("linen:write")),
):
    oid = _parse_object_id(doc_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = datetime.now(timezone.utc)

    laundry_id = resolve_laundry_id(current_user)
    await _owned_linen(oid, current_user)
    result = await linens_collection.update_one(owned_filter(oid, laundry_id), {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Linen not found")

    doc = await linens_collection.find_one({"_id": oid})
    await _log_audit(
        current_user.get("user_id") or current_user.get("auth_id", ""),
        "update",
        "linen",
        doc_id,
        laundry_id,
    )
    return _serialize(doc)


# ============================================================
#  DELETE linen
# ============================================================
@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_linen(
    doc_id: str,
    current_user: dict = Depends(require_capability("linen:write")),
):
    oid = _parse_object_id(doc_id)
    laundry_id, _ = await _owned_linen(oid, current_user)
    # Bump the version while the document still exists, then remove it and
    # enqueue a DELETE so the SECONDARY replica drops the record too.
    try:
        new_version = await bump_version("linen", oid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Linen not found")
    result = await linens_collection.delete_one(owned_filter(oid, laundry_id))
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Linen not found")
    await enqueue_delete("linen", oid, new_version)
    await _log_audit(
        current_user.get("user_id") or current_user.get("auth_id", ""),
        "delete",
        "linen",
        doc_id,
        laundry_id,
    )


# ============================================================
#  SCAN / ACTION — update status and create event
# ============================================================
@router.post("/{doc_id}/scan")
async def scan_linen(
    doc_id: str,
    payload: LinenScanAction,
    current_user: dict = Depends(require_capability("linen:scan")),
):
    oid = _parse_object_id(doc_id)
    laundry_id, doc = await _owned_linen(oid, current_user)

    action = payload.action.lower()
    now = datetime.now(timezone.utc)
    from_status = doc.get("status", "IN_STOCK")

    # Map action to new status
    ACTION_STATUS_MAP = {
        "collect": "COLLECTED",
        "receive": "AT_LAUNDRY",
        "start_wash": "WASHING",
        "complete_wash": "DRYING",
        "press": "PRESSING",
        "ready": "READY",
        "deliver": "DELIVERED",
        "mark_missing": "MISSING",
        "mark_damaged": "DAMAGED",
        "retire": "RETIRED",
    }

    to_status = ACTION_STATUS_MAP.get(action)
    if not to_status:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    update_fields: dict = {
        "status": to_status,
        "last_scanned_date": now,
        "updated_at": now,
    }

    # Update wash count when completing wash
    if action == "complete_wash":
        update_fields["wash_count"] = doc.get("wash_count", 0) + 1
        update_fields["last_washed_date"] = now

    # Set retirement date
    if action == "retire":
        update_fields["retirement_date"] = now

    # Set location if provided
    if payload.location:
        update_fields["location"] = payload.location

    await linens_collection.update_one(owned_filter(oid, laundry_id), {"$set": update_fields})

    # Create event record (audit trail — never overwritten)
    event_doc = {
        "laundry_id": laundry_id,
        "linen_id": doc["linen_id"],
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "location": payload.location,
        "user": payload.user or current_user.get("user_id", ""),
        "related_order": payload.related_order,
        "notes": payload.notes,
        "timestamp": now,
    }
    await linen_events_collection.insert_one(event_doc)

    await _log_audit(current_user.get("user_id", ""), f"scan:{action}", "linen", doc_id)

    # Return updated linen
    updated_doc = await linens_collection.find_one({"_id": oid})
    return _serialize(updated_doc)


# ============================================================
#  BULK SCAN — process multiple linens quickly
# ============================================================
@router.post("/bulk-scan")
async def bulk_scan_linens(
    linen_codes: list[str],
    action: str,
    location: Optional[str] = None,
    user: Optional[str] = None,
    current_user: dict = Depends(require_capability("linen:scan")),
):
    action_lower = action.lower()
    ACTION_STATUS_MAP = {
        "collect": "COLLECTED",
        "receive": "AT_LAUNDRY",
        "start_wash": "WASHING",
        "complete_wash": "DRYING",
        "press": "PRESSING",
        "ready": "READY",
        "deliver": "DELIVERED",
        "mark_missing": "MISSING",
        "mark_damaged": "DAMAGED",
        "retire": "RETIRED",
    }
    to_status = ACTION_STATUS_MAP.get(action_lower)
    if not to_status:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    now = datetime.now(timezone.utc)
    laundry_id = resolve_laundry_id(current_user)
    results = []
    errors = []

    for code in linen_codes:
        code = code.strip().upper()
        doc = await linens_collection.find_one({"linen_id": code})
        assert_same_laundry(doc, laundry_id, "Linen")
        if not doc:
            errors.append({"linen_id": code, "error": "Not found"})
            continue

        from_status = doc.get("status", "IN_STOCK")
        update_fields: dict = {
            "status": to_status,
            "last_scanned_date": now,
            "updated_at": now,
        }
        if action_lower == "complete_wash":
            update_fields["wash_count"] = doc.get("wash_count", 0) + 1
            update_fields["last_washed_date"] = now
        if action_lower == "retire":
            update_fields["retirement_date"] = now
        if location:
            update_fields["location"] = location

        await linens_collection.update_one(owned_filter(doc["_id"], laundry_id), {"$set": update_fields})

        event_doc = {
            "laundry_id": laundry_id,
            "linen_id": code,
            "action": action_lower,
            "from_status": from_status,
            "to_status": to_status,
            "location": location,
            "user": user or current_user.get("user_id", ""),
            "related_order": None,
            "notes": None,
            "timestamp": now,
        }
        await linen_events_collection.insert_one(event_doc)
        results.append({"linen_id": code, "from_status": from_status, "to_status": to_status})

    await _log_audit(current_user.get("user_id", ""), f"bulk_scan:{action}", "linen", f"count={len(results)}")

    return {"processed": results, "errors": errors, "total_processed": len(results), "total_errors": len(errors)}


# ============================================================
#  EVENTS — history for a linen
# ============================================================
@router.get("/{doc_id}/events", response_model=LinenEventListResponse)
async def get_linen_events(
    doc_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(require_capability("linen:read")),
):
    oid = _parse_object_id(doc_id)
    _, doc = await _owned_linen(oid, current_user)

    query = {"linen_id": doc["linen_id"]}
    total = await linen_events_collection.count_documents(query)
    cursor = linen_events_collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
    items = []
    async for evt in cursor:
        items.append(_serialize(evt))

    return LinenEventListResponse(items=items, total=total)


# ============================================================
#  STATS — dashboard summary
# ============================================================
@router.get("/stats/summary", response_model=LinenStats)
async def get_linen_stats(
    current_user: dict = Depends(require_capability("linen:read")),
):
    laundry_id = resolve_laundry_id(current_user)
    match = scoped_query({}, laundry_id)
    pipeline = (
        [{"$match": match}] if match else []
    ) + [
        {"$group": {
            "_id": "$status",
            "count": {"$sum": 1},
            "wash_total": {"$sum": "$wash_count"},
        }}
    ]
    results = await linens_collection.aggregate(pipeline).to_list(100)

    status_counts = {}
    total_wash = 0
    for r in results:
        status_counts[r["_id"]] = r["count"]
        total_wash += r.get("wash_total", 0)

    total = sum(status_counts.values())

    # Recently scanned (last 24h)
    yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
    recently = await linens_collection.count_documents(
        scoped_query({"last_scanned_date": {"$gte": yesterday}}, laundry_id)
    )

    return LinenStats(
        total=total,
        in_stock=status_counts.get("IN_STOCK", 0),
        at_client=status_counts.get("AT_CLIENT", 0),
        collected=status_counts.get("COLLECTED", 0),
        at_laundry=status_counts.get("AT_LAUNDRY", 0),
        washing=status_counts.get("WASHING", 0),
        drying=status_counts.get("DRYING", 0),
        pressing=status_counts.get("PRESSING", 0),
        ready=status_counts.get("READY", 0),
        delivered=status_counts.get("DELIVERED", 0),
        missing=status_counts.get("MISSING", 0),
        damaged=status_counts.get("DAMAGED", 0),
        retired=status_counts.get("RETIRED", 0),
        total_wash_cycles=total_wash,
        recently_scanned=recently,
    )


# ============================================================
#  TAG GENERATION — create linens and return their IDs
# ============================================================
@router.post("/generate-tags", status_code=status.HTTP_201_CREATED)
async def generate_linen_tags(
    payload: LinenTagGenerate,
    current_user: dict = Depends(require_capability("linen:write")),
):
    now = datetime.now(timezone.utc)
    laundry_id = resolve_laundry_id(current_user)
    created = []
    linen_ids = set()

    for _ in range(payload.quantity):
        for _attempt in range(10):
            lid = generate_linen_id()
            if lid not in linen_ids:
                existing = await linens_collection.find_one({"linen_id": lid})
                if not existing:
                    linen_ids.add(lid)
                    break
        else:
            raise HTTPException(status_code=500, detail="Failed to generate unique ID")

        doc = {
            "laundry_id": laundry_id,
            "linen_id": lid,
            "category": payload.category,
            "item_type": payload.item_type,
            "description": None,
            "size": payload.size,
            "color": payload.color,
            "client_name": payload.client_name,
            "department": payload.department,
            "status": "IN_STOCK",
            "condition": "NEW",
            "location": None,
            "wash_count": 0,
            "last_washed_date": None,
            "last_scanned_date": None,
            "retirement_date": None,
            "notes": None,
            "created_at": now,
            "updated_at": now,
        }
        result = await linens_collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        created.append(_serialize(doc))

    await _log_audit(current_user.get("user_id", ""), "generate_tags", "linen", f"count={len(created)}")

    return {"items": created, "count": len(created)}

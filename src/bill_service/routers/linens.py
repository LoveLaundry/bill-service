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
from ..live_clock import bump_live_clock, read_live_clock
from ..realtime import actor_of, linen_snapshot, publish_linen_event
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
    LinenTransition,
    LinenTransitionGroup,
    LinenTransitionList,
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


async def _note_change(
    laundry_id: str,
    reason: str,
    *,
    event: str,
    linen: dict | None = None,
    action: str | None = None,
    actor: dict | None = None,
    count: int | None = None,
    linen_ids: list | None = None,
    at: datetime | None = None,
) -> int:
    """Advance the live clock and push the change to every open screen.

    The clock is what a client that fell behind compares against after a
    reconnect; the socket event is what keeps a client that is already
    connected current. Both are bumped in the same place so no mutation can do
    one without the other.
    """
    clock = await bump_live_clock(laundry_id, reason)
    publish_linen_event(
        event=event,
        laundry_id=laundry_id,
        clock=clock,
        linen=linen,
        action=action,
        actor=actor,
        count=count,
        linen_ids=linen_ids,
        at=at,
    )
    return clock


async def _log_audit(user_id: str, action: str, entity: str, entity_id: str, laundry_id: str | None = None):
    # One writer, one schema — see router_utils.log_audit.
    from ..router_utils import log_audit as canonical_log_audit
    return await canonical_log_audit(user_id, action, entity, entity_id, None, None, laundry_id)


# --- Scan action contract ---
# The single definition of what a scan action does. It used to be re-typed
# inline inside `scan_linen` and `bulk_scan_linens`, where the two copies could
# drift apart without anything noticing. `GET /linens/transitions` publishes it
# so no client has to keep a third copy.
ACTION_STATUS_MAP: dict = {
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

# Which side of the HOTEL -> LAUNDRY -> HOTEL cycle performs each action.
# Derived from the operational meaning of the existing statuses; it adds no
# state and renames nothing. Exceptions are grouped apart because they are not
# a step in the cycle — see docs/linen-lifecycle-investigation.md §3.
ACTION_CUSTODY: dict = {
    "collect": "hotel",
    "receive": "laundry",
    "start_wash": "laundry",
    "complete_wash": "laundry",
    "press": "laundry",
    "ready": "laundry",
    "deliver": "laundry",
    "retire": "laundry",
    "mark_missing": "exception",
    "mark_damaged": "exception",
}

CUSTODY_GROUPS: list = [
    {
        "custody": "hotel",
        "label": "Hotel Hands",
        "description": "Linens leaving a hotel for the laundry",
    },
    {
        "custody": "laundry",
        "label": "Laundry Hands",
        "description": "Processing at the laundry and return to the hotel",
    },
    {
        "custody": "exception",
        "label": "Exceptions",
        "description": "Handover discrepancies, recorded without a cycle step",
    },
]

# A retired linen has no further lifecycle; a missing/damaged one does.
TERMINAL_ACTIONS = {"retire"}

# Action labels are UI text but belong next to the action they describe.
ACTION_LABELS: dict = {
    "collect": "Collect",
    "receive": "Receive",
    "start_wash": "Start Wash",
    "complete_wash": "Complete Wash",
    "press": "Press",
    "ready": "Ready",
    "deliver": "Deliver",
    "mark_missing": "Mark Missing",
    "mark_damaged": "Mark Damaged",
    "retire": "Retire",
}


def _status_label(status_value: str) -> str:
    """`AT_LAUNDRY` -> `At Laundry`. Humanising here keeps the vocabulary and
    its display name in the same place."""
    return status_value.replace("_", " ").title()


def _transition_catalogue() -> list:
    by_custody: dict = {g["custody"]: [] for g in CUSTODY_GROUPS}
    for action, to_status in ACTION_STATUS_MAP.items():
        by_custody[ACTION_CUSTODY[action]].append(
            LinenTransition(
                action=action,
                label=ACTION_LABELS[action],
                to_status=to_status,
                to_status_label=_status_label(to_status),
                custody=ACTION_CUSTODY[action],
                terminal=action in TERMINAL_ACTIONS,
            )
        )
    return [
        LinenTransitionGroup(**group, transitions=by_custody[group["custody"]])
        for group in CUSTODY_GROUPS
    ]


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
    await _note_change(
        laundry_id,
        "create",
        event="create",
        linen=linen_snapshot(doc),
        actor=actor_of(current_user),
        at=now,
    )

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
    await _note_change(
        laundry_id,
        "bulk_create",
        event="create_bulk",
        actor=actor_of(current_user),
        count=len(created),
        # Codes only: a screen showing the list needs to know the totals moved,
        # no open screen follows a tag that did not exist a moment ago.
        linen_ids=[item["linen_id"] for item in created],
        at=now,
    )

    return {"items": created, "count": len(created)}


# ============================================================
#  LIVE CLOCK — other sessions poll this to refetch instantly
# ============================================================
@router.get("/live-clock")
async def get_live_clock(
    current_user: dict = Depends(require_capability("linen:read")),
):
    laundry_id = resolve_laundry_id(current_user)
    return {"clock": await read_live_clock(laundry_id)}


# Declared before `/{doc_id}` on purpose: a single-segment path added after it
# would be captured by the document-id route and answered 400 "Invalid linen
# document ID". Same reason `/live-clock` sits up here.
@router.get("/transitions", response_model=LinenTransitionList)
async def list_transitions(
    current_user: dict = Depends(require_capability("linen:read")),
):
    """The scan actions the server accepts, grouped by which side performs them.

    Read-only and identical for every laundry — it describes the state machine,
    not the tenant's data. Clients build their state pickers from this so the
    status vocabulary has exactly one owner. Transition *legality* is not
    expressed here because the server does not enforce any yet.
    """
    return LinenTransitionList(groups=_transition_catalogue(), statuses=list(LINEN_STATUSES))


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
    code = str(linen_code or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="A tag code is required")

    # Scoped in the query, not filtered afterwards. An unscoped lookup reads
    # whichever document Mongo returns first, so a code that also exists in
    # another laundry could shadow this laundry's own tag and 404 it.
    doc = await linens_collection.find_one(
        scoped_query({"linen_id": code}, laundry_id)
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Linen not found")
    assert_same_laundry(doc, laundry_id, "Linen")

    # Update last_scanned_date
    now = datetime.now(timezone.utc)
    await linens_collection.update_one(
        owned_filter(doc["_id"], laundry_id),
        {"$set": {"last_scanned_date": now, "updated_at": now}},
    )
    doc["last_scanned_date"] = now
    # No clock bump: a lookup changes no tracked field. Bumping here made every
    # scan force a refetch on every screen in the laundry for no state change.
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
    # The pre-edit document is the only way to tell a screen *what changed*:
    # `LinenUpdate` can move `status` directly, so an edit is as much a status
    # change as a scan is.
    _, before = await _owned_linen(oid, current_user)
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
    await _note_change(
        laundry_id,
        "update",
        event="update",
        linen=linen_snapshot(doc, extra={"previous_status": before.get("status")}),
        actor=actor_of(current_user),
        at=updates["updated_at"],
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
    laundry_id, doomed = await _owned_linen(oid, current_user)
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
    await _note_change(
        laundry_id,
        "delete",
        event="delete",
        linen=linen_snapshot(doomed),
        actor=actor_of(current_user),
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

    # Publish after the read so the event carries the authoritative post-scan
    # row: every other screen patches its cache from this and never refetches.
    await _note_change(
        laundry_id,
        f"scan:{action}",
        event="scan",
        action=action,
        linen=linen_snapshot(updated_doc, extra={"previous_status": from_status}),
        actor=actor_of(current_user),
        at=now,
    )
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
    to_status = ACTION_STATUS_MAP.get(action_lower)
    if not to_status:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    now = datetime.now(timezone.utc)
    laundry_id = resolve_laundry_id(current_user)
    results = []
    errors = []
    pushed: list = []

    for code in linen_codes:
        code = str(code or "").strip().upper()
        if not code:
            continue
        # Scoped query, same reasoning as the single-tag lookup above. A foreign
        # or missing code resolves to None and is reported per item; calling
        # assert_same_laundry before the existence check raised 404 for the whole
        # batch and lost every valid code alongside the bad one.
        doc = await linens_collection.find_one(
            scoped_query({"linen_id": code}, laundry_id)
        )
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
        # Captured now, published after the single clock bump below, so every
        # item event of the batch carries the same clock value.
        pushed.append(
            linen_snapshot({**doc, **update_fields}, extra={"previous_status": from_status})
        )

    await _log_audit(current_user.get("user_id", ""), f"bulk_scan:{action}", "linen", f"count={len(results)}")
    if results:
        actor = actor_of(current_user)
        clock = await _note_change(
            laundry_id,
            f"bulk_scan:{action_lower}",
            event="bulk_scan",
            action=action_lower,
            actor=actor,
            count=len(results),
            linen_ids=[row["linen_id"] for row in results],
            at=now,
        )
        for snapshot in pushed:
            publish_linen_event(
                event="scan",
                laundry_id=laundry_id,
                clock=clock,
                action=action_lower,
                linen=snapshot,
                actor=actor,
                at=now,
            )

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


# ============================================================
#  SCAN ACTIVITY REPORT (laundry side of the QR lifecycle)
# ============================================================


# ============================================================
#  PUBLIC TRACK — what a QR scan on a garment tag resolves to
# ============================================================
@router.get("/track/{linen_code}")
async def public_linen_track(linen_code: str):
    """Unauthenticated, sanitized status for a scanned tag.

    Deliberately narrow: a guest scanning a tag learns the item and where it
    is in the wash cycle — never the owning hotel, location notes, or staff.
    """
    code = str(linen_code or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="A tag code is required")
    # Codes are random LL-XXXXXX across the whole deployment; if a collision
    # ever exists we refuse rather than guess which laundry's garment it is.
    matches = await linens_collection.find({"linen_id": code}).limit(2).to_list(2)
    if not matches:
        raise HTTPException(status_code=404, detail="Tag not found")
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="Tag code is ambiguous")
    doc = matches[0]
    return {
        "linen_id": doc.get("linen_id"),
        "category": doc.get("category"),
        "item_type": doc.get("item_type"),
        "status": doc.get("status"),
        "condition": doc.get("condition"),
        "wash_count": doc.get("wash_count", 0),
        "last_scanned_date": doc.get("last_scanned_date"),
        "updated_at": doc.get("updated_at"),
    }


@router.get("/reports/scan-activity")
async def scan_activity_report(
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(require_capability("linen:read")),
):
    """Aggregated QR scan report for the laundry.

    Every physical tag scan lands in ``linen_events``; this rolls them up by
    action and destination status, per day, plus the most recent scans — the
    operational counterpart of the hotel portal's per-tag view.
    """
    from ..app_time import day_bounds, parse_day
    from ..database.main_db import linen_events_collection

    laundry_id = resolve_laundry_id(current_user)
    query: dict = {"laundry_id": laundry_id}
    if date_from or date_to:
        try:
            start = day_bounds(parse_day(date_from))[0] if date_from else None
            end = day_bounds(parse_day(date_to))[1] if date_to else None
        except ValueError:
            raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD")
        rng = {}
        if start:
            rng["$gte"] = start
        if end:
            rng["$lt"] = end
        query["timestamp"] = rng

    by_action: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_day: Dict[str, int] = {}
    recent = []
    total = 0
    async for evt in linen_events_collection.find(query).sort("timestamp", -1):
        total += 1
        action = str(evt.get("action") or "unknown")
        by_action[action] = by_action.get(action, 0) + 1
        to_status = str(evt.get("to_status") or "")
        if to_status:
            by_status[to_status] = by_status.get(to_status, 0) + 1
        ts = evt.get("timestamp")
        if ts is not None:
            day = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            by_day[day] = by_day.get(day, 0) + 1
        if len(recent) < limit:
            recent.append({
                "linen_id": evt.get("linen_id"),
                "action": action,
                "from_status": evt.get("from_status"),
                "to_status": evt.get("to_status"),
                "user": evt.get("user"),
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else ts,
            })

    return {
        "total_scans": total,
        "by_action": by_action,
        "by_status": by_status,
        "by_day": dict(sorted(by_day.items())),
        "recent": recent,
    }


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
    await _note_change(
        laundry_id,
        "generate_tags",
        event="generate_tags",
        actor=actor_of(current_user),
        count=len(created),
        linen_ids=[item["linen_id"] for item in created],
        at=now,
    )

    return {"items": created, "count": len(created)}

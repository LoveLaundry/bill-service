from datetime import datetime, timezone
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from bson import ObjectId
from bson.errors import InvalidId

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import (
    audit_collection,
    deliveries_collection,
    gatepasses_collection,
    returns_collection,
)
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services import idempotency
from ..error_responses import NotFoundError, ValidationError, ConflictError, ForbiddenError
from ..services.bill_sync import sync_bills_to_gate_pass
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_CATCH_UP_DELIVERY,
    EVENT_GATE_PASS_CREATED,
    EVENT_LEGACY_FLAG,
    EVENT_RECEIVING_DATE_CHANGED,
    EVENT_RECEIVING_EDITED,
    EVENT_STATUS_CHANGED,
)
from ..services.verification_service import attach_verification_to
from ..models import (
    GatePassAdjustment,
    GatePassAdjustmentRequest,
    GatePassCatchUpDelivery,
    GatePassCreate,
    GatePassDateUpdate,
    GatePassMarkDelivered,
    GatePassModel,
    GatePassUpdate,
)
from .adjustments import create_adjustment_request

router = APIRouter(prefix="/gatepasses", tags=["gatepasses"])

SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def _serialize(doc: dict) -> dict:
    try:
        decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to decrypt document: {str(e)}"
        )

    decrypted["id"] = str(decrypted["_id"])
    del decrypted["_id"]
    if not isinstance(decrypted.get("items"), list):
        decrypted["items"] = []
    return decrypted


def _parse_object_id(gate_pass_id: str) -> ObjectId:
    try:
        return ObjectId(gate_pass_id)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid gate pass ID"
        )


async def log_audit(user_id: str, action: str, entity: str, entity_id: str):
    await audit_collection.insert_one(
        {
            "user_id": user_id,
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "timestamp": datetime.now(timezone.utc),
        }
    )


@router.post(
    "", response_model=GatePassModel, status_code=status.HTTP_201_CREATED
)
async def create_gate_pass(
    payload: GatePassCreate,
    current_user: dict = Depends(require_capability("gatepass:write")),
    request: Request = None,
):
    auth_id = current_user.get("auth_id", "system")

    # Idempotent create: a retry with the same X-Idempotency-Key returns the
    # previously created gate pass instead of duplicating it.
    existing_created = await idempotency.find_previous(request, auth_id, gatepasses_collection)
    if existing_created:
        return _serialize(existing_created)

    # Check if uniqueness constraint violates
    existing = await gatepasses_collection.find_one(
        {"gate_pass_number": payload.gate_pass_number}
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gate Pass number already exists",
        )

    # Process items and calculate differences
    processed_items = []
    for item in payload.items:
        diff = item.received_qty - item.client_qty
        processed_items.append(
            {
                "item_name": item.item_name,
                "category": item.category,
                "specification": item.specification,
                "client_qty": item.client_qty,
                "received_qty": item.received_qty,
                "difference": diff,
                "mismatch_reason": item.mismatch_reason,
                "mismatch_notes": item.mismatch_notes,
            }
        )

    now = datetime.now(timezone.utc)
    doc = {
        "gate_pass_number": payload.gate_pass_number,
        "client_name": payload.client_name,
        "receiving_date": payload.receiving_date.replace(tzinfo=timezone.utc),
        "received_by": payload.received_by,
        "items": processed_items,
        "status": "RECEIVED",
        "notes": payload.notes,
        "quotation_id": payload.quotation_id,
        "created_at": now,
        "updated_at": now,
        "adjustments": [],
    }

    # Envelope Encrypt Document
    encrypted_doc = encrypt_dict(doc, SENSITIVE_FIELDS)

    result = await gatepasses_collection.insert_one(encrypted_doc)
    created = await gatepasses_collection.find_one({"_id": result.inserted_id})

    serialized = _serialize(created)

    new_version = await bump_version("gatepass", result.inserted_id)
    await enqueue_sync("gatepass", result.inserted_id, new_version)
    serialized = await attach_verification_to("gatepass", result.inserted_id, serialized)

    await record_event(
        entity_type="gatepass",
        entity_id=serialized["id"],
        event_type=EVENT_GATE_PASS_CREATED,
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        new_status="RECEIVED",
        item_deltas=[
            build_item_delta(item["item_name"], item.get("specification"), 0, item["received_qty"])
            for item in processed_items
        ],
        meta={"gate_pass_number": payload.gate_pass_number},
    )

    await log_audit(
        current_user.get("auth_id", "system"),
        "RECEIVING_CREATE",
        "gatepass",
        serialized["id"],
    )
    await idempotency.record_created(request, auth_id, "gatepass", serialized["id"])
    return serialized


@router.get("", response_model=List[GatePassModel])
async def list_gate_passes(
    client_name: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    query = {}

    if client_name:
        # Search using HMAC token
        query["client_name_search"] = get_search_token(client_name)

    if status_filter:
        query["status"] = status_filter

    if date_from or date_to:
        date_query = {}
        if date_from:
            date_query["$gte"] = date_from.replace(tzinfo=timezone.utc)
        if date_to:
            date_query["$lte"] = date_to.replace(tzinfo=timezone.utc)
        query["receiving_date"] = date_query

    cursor = gatepasses_collection.find(query).sort("receiving_date", -1)
    results = []
    async for doc in cursor:
        try:
            serialized = _serialize(doc)
            results.append(await attach_verification_to("gatepass", doc["_id"], serialized))
        except HTTPException:
            pass  # skip if decryption fails
    return results


@router.get("/{gate_pass_id}", response_model=GatePassModel)
async def get_gate_pass(
    gate_pass_id: str,
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )
    serialized = _serialize(doc)
    return await attach_verification_to("gatepass", oid, serialized)


@router.get("/{gate_pass_id}/balance")
async def get_gate_pass_balance(
    gate_pass_id: str,
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """Canonical per-gate-pass balance computed by the balance engine.

    Every screen that renders pending/remaining/delivered for a gate pass
    should consume this endpoint instead of re-calculating locally.
    """
    from ..services import balance_engine as be

    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )
    decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)

    deliveries: List[dict] = []
    dl_cursor = deliveries_collection.find(
        {"gate_pass_id": gate_pass_id, "status": {"$ne": "CANCELLED"}}
    )
    async for dl_doc in dl_cursor:
        try:
            deliveries.append(decrypt_dict(dl_doc, SENSITIVE_FIELDS))
        except Exception:
            continue

    returns: List[dict] = []
    ret_cursor = returns_collection.find({"gate_pass_id": gate_pass_id})
    async for ret_doc in ret_cursor:
        try:
            returns.append(decrypt_dict(ret_doc, SENSITIVE_FIELDS))
        except Exception:
            continue

    balance = be.compute_gate_pass_balance(
        decrypted.get("items", []),
        be.compute_delivered_by_item(deliveries),
        be.compute_returned_by_item(returns),
        marked_delivered=bool(decrypted.get("marked_delivered")),
    )

    derived_status = be.derive_gate_pass_status(balance, decrypted.get("status", ""))

    return {
        "gate_pass_id": gate_pass_id,
        "gate_pass_number": decrypted.get("gate_pass_number"),
        "client_name": decrypted.get("client_name"),
        "receiving_date": decrypted.get("receiving_date"),
        "status": decrypted.get("status"),
        "derived_status": derived_status,
        "marked_delivered": bool(decrypted.get("marked_delivered")),
        "items": list(balance["items"].values()),
        "totals": balance["totals"],
        "flags": balance["flags"],
    }


@router.patch("/{gate_pass_id}/status", response_model=GatePassModel)
async def update_gate_pass_status(
    gate_pass_id: str,
    status_update: str = Query(...),
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Guarded status transition.

    Only the open workflow states can be moved manually:
      RECEIVED -> PROCESSING -> READY_FOR_DELIVERY (and back to PROCESSING)
      any open state -> CANCELLED (only if no deliveries were recorded)
    DELIVERED / PARTIALLY_DELIVERED / CLOSED are DERIVED from recorded
    quantities and can never be set by hand; reversing them requires the
    controlled reversal flow.
    """
    if status_update in ("DELIVERED", "CLOSED", "PARTIALLY_DELIVERED"):
        raise HTTPException(
            status_code=400,
            detail=(
                "DELIVERED / PARTIALLY_DELIVERED / CLOSED are derived from actual "
                "recorded quantities and cannot be set manually. Use a real "
                "quantity-based delivery (or catch-up delivery) instead."
            ),
        )

    allowed_transitions = {
        "RECEIVED": ["PROCESSING", "CANCELLED"],
        "PROCESSING": ["READY_FOR_DELIVERY", "CANCELLED"],
        "READY_FOR_DELIVERY": ["PROCESSING", "CANCELLED"],
    }

    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )

    previous_status = doc.get("status")
    if previous_status not in allowed_transitions:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Gate Pass is '{previous_status}' which cannot be moved manually. "
                "Open/derived states must change through recorded quantities."
            ),
        )
    if status_update not in allowed_transitions[previous_status]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition {previous_status} -> {status_update}.",
        )

    if status_update == "CANCELLED":
        existing_deliveries = await deliveries_collection.count_documents(
            {"gate_pass_id": gate_pass_id, "status": {"$ne": "CANCELLED"}}
        )
        if existing_deliveries > 0:
            raise HTTPException(
                status_code=409,
                detail="Cannot cancel a gate pass that already has delivery records. "
                "Record a reversal instead.",
            )

    # Update status field
    await gatepasses_collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": status_update,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    updated_doc = await gatepasses_collection.find_one({"_id": oid})
    serialized = _serialize(updated_doc)

    new_version = await bump_version("gatepass", oid)
    await enqueue_sync("gatepass", oid, new_version)
    serialized = await attach_verification_to("gatepass", oid, serialized)

    await record_event(
        entity_type="gatepass",
        entity_id=serialized["id"],
        event_type=EVENT_STATUS_CHANGED,
        gate_pass_id=serialized["id"],
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        prev_status=previous_status,
        new_status=status_update,
        meta={"endpoint": "status_patch"},
    )

    await log_audit(
        current_user.get("auth_id", "system"),
        "RECEIVING_STATUS_UPDATE",
        "gatepass",
        serialized["id"],
    )
    return serialized


@router.post("/{gate_pass_id}/mark-delivered", response_model=GatePassModel)
async def mark_gate_pass_delivered(
    gate_pass_id: str,
    payload: GatePassMarkDelivered,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Complete a gate pass that was delivered but the delivery was never recorded.

    Used when the manager forgot to record the dispatch on the delivery date.
    Requires a mandatory note; stores the catch-up record on the gate pass so
    it is treated as fully delivered while keeping an audit trail.
    """
    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )

    current_status = doc.get("status")
    if current_status in ("DELIVERED", "CANCELLED"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Gate Pass is already {current_status} and cannot be marked delivered.",
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "Note-based delivery confirmation is no longer allowed. A note "
            "cannot create a delivery or close a balance. Use "
            "POST /gatepasses/{id}/catch-up-delivery with explicit item "
            "quantities so the delivered quantities are recorded."
        ),
    )


@router.post("/{gate_pass_id}/catch-up-delivery", response_model=GatePassModel)
async def catch_up_delivery(
    gate_pass_id: str,
    payload: GatePassCatchUpDelivery,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Quantity-based catch-up delivery (replaces the mark-delivered note flow).

    Creates a REAL delivery record with explicit per-item quantities. The
    gate pass status is then DERIVED from the recorded quantities — a note
    alone can never close it.
    """
    from ..services import balance_engine as be

    oid = _parse_object_id(gate_pass_id)
    gp_doc = await gatepasses_collection.find_one({"_id": oid})
    if not gp_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )
    decrypted = decrypt_dict(gp_doc, SENSITIVE_FIELDS)
    current_status = decrypted.get("status")
    if current_status in ("DELIVERED", "CANCELLED"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Gate Pass is already {current_status}. Already-closed passes "
                "need a controlled reversal, not another delivery."
            ),
        )

    # Load existing deliveries to validate the catch-up quantities per item.
    existing_deliveries: List[dict] = []
    dl_cursor = deliveries_collection.find(
        {"gate_pass_id": gate_pass_id, "status": {"$ne": "CANCELLED"}}
    )
    async for dl_doc in dl_cursor:
        try:
            existing_deliveries.append(decrypt_dict(dl_doc, SENSITIVE_FIELDS))
        except Exception:
            continue

    delivered_map = be.compute_delivered_by_item(existing_deliveries)
    received_map: Dict[str, int] = {}
    for it in decrypted.get("items", []):
        key = be.item_key(it["item_name"], it.get("specification"))
        received_map[key] = received_map.get(key, 0) + it["received_qty"]

    now = datetime.now(timezone.utc)
    delivered_date = payload.delivered_date or now
    if delivered_date.tzinfo is None:
        delivered_date = delivered_date.replace(tzinfo=timezone.utc)

    # Validate quantities against available balance (received - already delivered).
    item_records = []
    for item in payload.items:
        key = be.item_key(item.item_name, item.specification)
        received = received_map.get(key, 0)
        if received <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"'{item.item_name}' was never received on this gate pass.",
            )
        already = delivered_map.get(key, 0)
        remaining = max(0, received - already)
        if item.quantity > remaining:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Catch-up quantity {item.quantity} for '{item.item_name}' "
                    f"exceeds available {remaining} (received {received}, already "
                    f"delivered {already})."
                ),
            )
        item_records.append(
            {"item_name": item.item_name, "specification": item.specification, "quantity": item.quantity}
        )

    marker = f"Catch-up delivery: {payload.note}"
    existing_notes = decrypted.get("notes") or ""
    delivery_doc = {
        "gate_pass_id": gate_pass_id,
        "client_name": decrypted.get("client_name"),
        "delivery_date": delivered_date,
        "delivered_by": current_user.get("user_name", "system"),
        "received_by": current_user.get("user_name", "system"),
        "items": item_records,
        "status": "DELIVERED",
        "notes": marker,
        "created_at": now,
        "catch_up": {"note": payload.note, "at": now, "user_id": current_user.get("auth_id", "system")},
    }
    encrypted_delivery = encrypt_dict(delivery_doc, SENSITIVE_FIELDS)
    dl_result = await deliveries_collection.insert_one(encrypted_delivery)

    # Derive new gate pass status from the recorded quantities.
    delivered_map = be.compute_delivered_by_item(existing_deliveries + [delivery_doc])
    balance = be.compute_gate_pass_balance(
        decrypted.get("items", []), delivered_map, {}, marked_delivered=False
    )
    new_gp_status = be.derive_gate_pass_status(balance, current_status)

    updated_gp = dict(decrypted)
    updated_gp["status"] = new_gp_status
    updated_gp["notes"] = f"{existing_notes}\n{marker}".strip() if existing_notes else marker
    updated_gp["updated_at"] = now
    encrypted_gp = encrypt_dict(updated_gp, SENSITIVE_FIELDS)
    await gatepasses_collection.replace_one({"_id": oid}, encrypted_gp)

    # Bump + sync both entities.
    dl_id = str(dl_result.inserted_id)
    dl_version = await bump_version("delivery", dl_result.inserted_id)
    await enqueue_sync("delivery", dl_result.inserted_id, dl_version)
    gp_version = await bump_version("gatepass", oid)
    await enqueue_sync("gatepass", oid, gp_version)

    await record_event(
        entity_type="delivery",
        entity_id=dl_id,
        event_type=EVENT_CATCH_UP_DELIVERY,
        gate_pass_id=gate_pass_id,
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=payload.note,
        item_deltas=[
            build_item_delta(it["item_name"], it.get("specification"), 0, it["quantity"])
            for it in item_records
        ],
        prev_status=current_status,
        new_status=new_gp_status,
        meta={"delivered_date": delivered_date.isoformat(), "catch_up": True},
    )

    await log_audit(
        current_user.get("auth_id", "system"),
        "RECEIVING_CATCH_UP_DELIVERY",
        "delivery",
        dl_id,
    )

    serialized = _serialize(await gatepasses_collection.find_one({"_id": oid}))
    return await attach_verification_to("gatepass", oid, serialized)


async def _reopen_legacy_doc(gp_doc: dict, current_user: dict) -> dict:
    """Reopen one legacy note-closed pass.

    Never raises for a specific pass; returns a result dict for callers to
    surface per-pass (single endpoint) or aggregate (batch endpoint).
    """
    oid = gp_doc["_id"]
    gp_id = str(oid)
    try:
        decrypted = decrypt_dict(gp_doc, SENSITIVE_FIELDS)

        marked = decrypted.get("marked_delivered")
        if not marked:
            return {"gate_pass_id": gp_id, "reopened": False, "reason": "no legacy note closure"}

        dl_cursor = deliveries_collection.find(
            {"gate_pass_id": gp_id, "status": {"$ne": "CANCELLED"}}
        )
        delivery_count = 0
        async for _dl in dl_cursor:
            delivery_count += 1
        if delivery_count > 0:
            return {
                "gate_pass_id": gp_id,
                "reopened": False,
                "reason": "has recorded deliveries (use a controlled reversal)",
            }

        current_status = decrypted.get("status", "RECEIVED")
        now = datetime.now(timezone.utc)
        legacy_date = None
        legacy_note = ""
        if isinstance(marked, dict):
            legacy_date = marked.get("delivered_date") or marked.get("at")
            legacy_note = str(marked.get("note") or "")
        elif isinstance(marked, bool) or isinstance(marked, int):
            # Older docs may have stored True with the note on the doc itself.
            legacy_note = str(decrypted.get("notes") or "")

        marker = (
            "Reopened for proper delivery recording (was closed by a legacy note"
            + (f" on {legacy_date}" if legacy_date else "")
            + "): "
            + (legacy_note or "no note was recorded")
        ).strip()

        updated_gp = dict(decrypted)
        updated_gp.pop("marked_delivered", None)
        updated_gp["status"] = "RECEIVED"
        existing_notes = decrypted.get("notes") or ""
        updated_gp["notes"] = f"{existing_notes}\n{marker}".strip() if existing_notes else marker
        updated_gp["updated_at"] = now
        encrypted_gp = encrypt_dict(updated_gp, SENSITIVE_FIELDS)
        await gatepasses_collection.replace_one({"_id": oid}, encrypted_gp)

        gp_version = await bump_version("gatepass", oid)
        await enqueue_sync("gatepass", oid, gp_version)

        await record_event(
            entity_type="gatepass",
            entity_id=gp_id,
            event_type=EVENT_LEGACY_FLAG,
            gate_pass_id=gp_id,
            user_id=current_user.get("auth_id", "system"),
            user_name=current_user.get("user_name"),
            reason="LEGACY_CLOSED_WITHOUT_DELIVERY",
            prev_status=current_status,
            new_status="RECEIVED",
            meta={
                "reopened": True,
                "legacy": True,
                "legacy_note": legacy_note,
                "legacy_date": legacy_date,
            },
        )

        await log_audit(
            current_user.get("auth_id", "system"),
            "REOPENED_LEGACY_GATEPASS",
            "gatepass",
            gp_id,
        )

        return {
            "gate_pass_id": gp_id,
            "gate_pass_number": decrypted.get("gate_pass_number"),
            "client_name": decrypted.get("client_name"),
            "reopened": True,
            "reason": None,
        }
    except Exception as exc:  # never let one bad pass fail the whole batch
        return {
            "gate_pass_id": gp_id,
            "reopened": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }


@router.post("/{gate_pass_id}/reopen", response_model=GatePassModel)
async def reopen_legacy_gate_pass(
    gate_pass_id: str,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Reopen a pass that was closed by the OLD mark-delivered note.

    Only eligible when the pass:
      * has a ``marked_delivered`` legacy note closure, AND
      * has ZERO real delivery records (so no recorded history is at risk).

    The legacy closure is flagged in the journal (LEGACY_FLAG with reason
    LEGACY_CLOSED_WITHOUT_DELIVERY) and the pass is moved back to RECEIVED so
    it re-enters the pending-to-deliver list. No quantities are fabricated.
    """
    oid = _parse_object_id(gate_pass_id)
    gp_doc = await gatepasses_collection.find_one({"_id": oid})
    if not gp_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )
    result = await _reopen_legacy_doc(gp_doc, current_user)
    if not result["reopened"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=result["reason"]
        )
    serialized = _serialize(await gatepasses_collection.find_one({"_id": oid}))
    return await attach_verification_to("gatepass", oid, serialized)


@router.post("/reopen-legacy")
async def reopen_all_legacy_gate_passes(
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Batch migration: reopen every legacy note-closed pass that is eligible.

    Idempotent and safe — only passes with a ``marked_delivered`` closure and
    no recorded deliveries change; everything else is skipped and reported.
    """
    reopened = []
    skipped = []
    gp_cursor = gatepasses_collection.find({})
    async for gp_doc in gp_cursor:
        if gp_doc.get("status") == "CANCELLED":
            continue
        result = await _reopen_legacy_doc(gp_doc, current_user)
        if result["reopened"]:
            reopened.append(result)
        else:
            skipped.append(result)
    return {"reopened": reopened, "skipped": skipped}


@router.patch("/{gate_pass_id}/date", response_model=GatePassModel)
async def update_gate_pass_date(
    gate_pass_id: str,
    payload: GatePassDateUpdate,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )

    await gatepasses_collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "receiving_date": payload.receiving_date,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    updated_doc = await gatepasses_collection.find_one({"_id": oid})
    serialized = _serialize(updated_doc)

    new_version = await bump_version("gatepass", oid)
    await enqueue_sync("gatepass", oid, new_version)
    serialized = await attach_verification_to("gatepass", oid, serialized)

    await record_event(
        entity_type="gatepass",
        entity_id=serialized["id"],
        event_type=EVENT_RECEIVING_DATE_CHANGED,
        gate_pass_id=serialized["id"],
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=payload.reason,
        meta={"receiving_date": payload.receiving_date.isoformat()},
    )

    await log_audit(
        current_user.get("auth_id", "system"),
        "RECEIVING_DATE_UPDATE",
        "gatepass",
        serialized["id"],
    )
    return serialized


@router.patch("/{gate_pass_id}", response_model=GatePassModel)
async def update_gate_pass(
    gate_pass_id: str,
    payload: GatePassUpdate,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    try:
        oid = _parse_object_id(gate_pass_id)
        doc = await gatepasses_collection.find_one({"_id": oid})
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
            )

        if doc.get("status") in ("DELIVERED", "PARTIALLY_DELIVERED", "CANCELLED"):
            raise HTTPException(
                status_code=409,
                detail=f"Gate Pass cannot be edited once it is {doc.get('status')}.",
            )

        decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
        update_data = payload.model_dump(exclude_unset=True)

        # Quantities are controlled: received/client quantities may never be
        # rewritten once deliveries or returns exist — use the adjustment flow.
        if "items" in update_data and update_data["items"]:
            has_movement = (
                await deliveries_collection.count_documents(
                    {"gate_pass_id": gate_pass_id, "status": {"$ne": "CANCELLED"}}
                )
                > 0
            ) or (
                await returns_collection.count_documents(
                    {"gate_pass_id": gate_pass_id}
                )
                > 0
            )
            if has_movement:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This gate pass already has delivery/return records. "
                        "Quantity changes must go through the controlled "
                        "adjustment workflow (POST /adjustments) so the original "
                        "record and history are preserved."
                    ),
                )

        previous_items = {
            f"{i.get('item_name', '')}||{i.get('specification') or ''}": int(
                i.get("received_qty", 0) or 0
            )
            for i in decrypted.get("items", [])
        }

        if "items" in update_data and update_data["items"]:
            processed_items = []
            for item in update_data["items"]:
                processed_items.append(
                    {
                        "item_name": item["item_name"],
                        "category": item.get("category"),
                        "specification": item.get("specification"),
                        "client_qty": item["client_qty"],
                        "received_qty": item["received_qty"],
                        "difference": item["received_qty"] - item["client_qty"],
                        "mismatch_reason": item.get("mismatch_reason"),
                        "mismatch_notes": item.get("mismatch_notes"),
                    }
                )
            decrypted["items"] = processed_items

        if "client_name" in update_data:
            decrypted["client_name"] = update_data["client_name"]
        if "received_by" in update_data:
            decrypted["received_by"] = update_data["received_by"]
        if "notes" in update_data:
            decrypted["notes"] = update_data["notes"]

        decrypted["updated_at"] = datetime.now(timezone.utc)

        encrypted_new = encrypt_dict(decrypted, SENSITIVE_FIELDS)
        await gatepasses_collection.replace_one({"_id": oid}, encrypted_new)

        updated_doc = await gatepasses_collection.find_one({"_id": oid})
        serialized = _serialize(updated_doc)

        new_version = await bump_version("gatepass", oid)
        await enqueue_sync("gatepass", oid, new_version)
        serialized = await attach_verification_to("gatepass", oid, serialized)

        # Automatic propagation: any linked, still-editable bill is re-clamped
        # to the corrected received quantities (GP-leg bills may exist even
        # while no deliveries/returns block this edit).
        if "items" in update_data and update_data["items"]:
            try:
                await sync_bills_to_gate_pass(
                    serialized["id"],
                    processed_items,
                    user_id=current_user.get("auth_id", "system"),
                    user_name=current_user.get("user_name"),
                    reason=update_data.get("notes"),
                )
            except Exception:
                import logging
                logging.getLogger("bill_service").exception(
                    "bill_sync failed after gate pass item edit %s", oid
                )

        old_items = previous_items
        if "items" in update_data and update_data["items"]:
            deltas = [
                build_item_delta(
                    item["item_name"],
                    item.get("specification"),
                    old_items.get(
                        f"{item['item_name']}||{item.get('specification') or ''}", 0
                    ),
                    item["received_qty"],
                )
                for item in processed_items
                if old_items.get(
                    f"{item['item_name']}||{item.get('specification') or ''}", 0
                )
                != item["received_qty"]
            ]
            await record_event(
                entity_type="gatepass",
                entity_id=serialized["id"],
                event_type=EVENT_RECEIVING_EDITED,
                gate_pass_id=serialized["id"],
                user_id=current_user.get("auth_id", "system"),
                user_name=current_user.get("user_name"),
                item_deltas=deltas or None,
                reason=update_data.get("notes"),
                meta={"changed_fields": [k for k in update_data if k != "items"]} or {"items": True},
            )
        else:
            await record_event(
                entity_type="gatepass",
                entity_id=serialized["id"],
                event_type=EVENT_RECEIVING_EDITED,
                gate_pass_id=serialized["id"],
                user_id=current_user.get("auth_id", "system"),
                user_name=current_user.get("user_name"),
                reason=update_data.get("notes"),
                meta={"changed_fields": [k for k in update_data if k != "items"]},
            )

        await log_audit(
            current_user.get("auth_id", "system"),
            "RECEIVING_UPDATE",
            "gatepass",
            serialized["id"],
        )
        return serialized
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update gate pass: {str(e)}",
        )


@router.post("/{gate_pass_id}/adjust", response_model=GatePassModel)
async def adjust_gate_pass(
    gate_pass_id: str,
    payload: GatePassAdjustment,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Request an item-quantity correction on a gate pass.

    This endpoint NEVER rewrites quantities in place — the previous version
    mutated the pass immediately, skipped the second-user approval gate, was
    unaware of item specifications, and never re-synced linked bills. It now
    stages a REQUESTED adjustment through the same controlled workflow as
    ``POST /adjustments``; the gate pass is returned UNCHANGED until another
    user approves the request (approved corrections re-sync linked bills).
    """
    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )

    request = GatePassAdjustmentRequest(
        gate_pass_id=gate_pass_id,
        item_name=payload.item_name,
        specification=payload.specification,
        corrected_qty=payload.corrected_qty,
        reason=payload.reason,
    )
    await create_adjustment_request(request, current_user)

    updated_doc = await gatepasses_collection.find_one({"_id": oid})
    serialized = _serialize(updated_doc)
    return await attach_verification_to("gatepass", oid, serialized)

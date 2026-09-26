"""Deliveries — items sent back to a hotel, with per-gate-pass traceability.

A delivery is NOT tied to one gate pass. Each line records the gate pass its
quantity came from, so:

  * one gate pass can be fulfilled by many deliveries (partial deliveries and
    post-correction balances), and
  * one delivery can draw lines from many gate passes.

Balances are always derived from current state by
:mod:`bill_service.services.balance_engine` — never accumulated from manual
adjustments — and every line is validated against the availability of the
specific pass it names before it is accepted.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..auth_helper import require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import (
    audit_collection,
    deliveries_collection,
    gatepasses_collection,
    returns_collection,
)
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services import idempotency
from ..services import balance_engine as be
from ..services import operations_context as ctx
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_DELIVERY_CANCELLED,
    EVENT_DELIVERY_CORRECTED,
    EVENT_DELIVERY_CREATED,
    EVENT_DELIVERY_DATE_CHANGED,
)
from ..services.verification_service import attach_verification_to
from ..models import (
    DeliveryCancel,
    DeliveryCorrection,
    DeliveryCreate,
    DeliveryDateUpdate,
    DeliveryModel,
)

router = APIRouter(prefix="/deliveries", tags=["deliveries"])

SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def _parse_object_id(id_str: str, label: str = "Invalid ID format") -> ObjectId:
    try:
        return ObjectId(id_str)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=label
        )


def _serialize(doc: dict) -> dict:
    try:
        decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to decrypt document: {str(e)}"
        )

    decrypted["id"] = str(decrypted["_id"])
    del decrypted["_id"]
    decrypted.setdefault("source_gate_pass_ids", [])
    if not decrypted["source_gate_pass_ids"]:
        decrypted["source_gate_pass_ids"] = [decrypted["gate_pass_id"]]
    decrypted.setdefault("corrections", [])
    items = decrypted.get("items")
    if not isinstance(items, list):
        decrypted["items"] = []
    return decrypted


def _client_mismatch_error(expected: str, actual: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"Hotel mismatch: this delivery is for '{expected}' but gate pass "
            f"{actual} belongs to a different hotel. A delivery can only draw "
            "from gate passes of the same hotel."
        ),
    )


async def log_audit(user_id: str, action: str, entity: str, entity_id: str, details: dict = None):
    doc = {
        "user_id": user_id,
        "action": action,
        "entity": entity,
        "entity_id": entity_id,
        "timestamp": datetime.now(timezone.utc),
    }
    if details:
        doc["details"] = details
    await audit_collection.insert_one(doc)


def _recompute_discrepancy(item: dict) -> int:
    """Server-owned reconciliation between recorded and client-counted qty."""
    counted = item.get("client_counted_qty")
    if counted is None:
        return 0
    return int(item.get("quantity", 0) or 0) - int(counted or 0)


def _source_filter(gate_pass_id: Optional[str]) -> dict:
    """Query filter matching every delivery that draws from a gate pass.

    Delegates to the shared context so list filters, per-gate-pass delivery
    listings and balance loading can never disagree about which deliveries
    belong to a pass.
    """
    return ctx.source_filter(gate_pass_id)


@router.post("", response_model=DeliveryModel, status_code=status.HTTP_201_CREATED)
async def create_delivery(
    payload: DeliveryCreate,
    current_user: dict = Depends(require_capability("delivery:write")),
    request: Request = None,
):
    auth_id = current_user.get("auth_id", "system")

    # Idempotent create: a retry with the same X-Idempotency-Key returns the
    # previously created delivery instead of duplicating it.
    existing_created = await idempotency.find_previous(request, auth_id, deliveries_collection)
    if existing_created:
        return _serialize(existing_created)

    # ── 1. Resolve the source gate passes for every line ──────────────────────
    requested = [item.model_dump() for item in payload.items]
    gp_ids: List[str] = []
    for line in requested:
        gp_id = str(line.get("gate_pass_id") or payload.gate_pass_id or "")
        if gp_id and gp_id not in gp_ids:
            gp_ids.append(gp_id)
    if payload.gate_pass_id and payload.gate_pass_id not in gp_ids:
        gp_ids.append(payload.gate_pass_id)

    if not gp_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No source gate pass. Every delivered item must state which gate "
                "pass it came from (set gate_pass_id on the delivery or on each item)."
            ),
        )

    gate_passes = {str(gp["gate_pass_id"]): gp for gp in await ctx.load_gate_passes(gp_ids)}
    missing = [g for g in gp_ids if g not in gate_passes]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Gate Pass not found: {', '.join(missing)}",
        )

    # ── 2. Hotel separation: every source pass must belong to this hotel ──────
    canonical_client = None
    for gp_id in gp_ids:
        gp_client = gate_passes[gp_id].get("client_name")
        if canonical_client is None:
            canonical_client = gp_client
        elif not be.same_client(canonical_client, gp_client):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "A delivery cannot mix hotels. Selected gate passes belong to "
                    f"'{canonical_client}' and '{gp_client}'. Record one delivery per hotel."
                ),
            )
    if not be.same_client(payload.client_name, canonical_client):
        raise _client_mismatch_error(payload.client_name, canonical_client)

    # ── 3. Validate every line against the pass it came from ─────────────────
    deliveries, returns = await ctx.load_movements(gp_ids)
    available = ctx.availability_maps(
        list(gate_passes.values()),
        ctx.delivered_by_gate_pass(deliveries),
        ctx.returned_by_gate_pass(returns),
    )

    try:
        planned = be.plan_delivery_lines(
            requested,
            gate_passes,
            available,
            default_gate_pass_id=payload.gate_pass_id,
        )
    except be.DeliveryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "errors": exc.errors},
        )

    for line in planned:
        line["discrepancy"] = _recompute_discrepancy(line)

    # ── 4. Persist ───────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    source_ids = list(dict.fromkeys(str(l["gate_pass_id"]) for l in planned))
    primary_gp_id = payload.gate_pass_id or source_ids[0]
    delivery_date = payload.delivery_date
    if delivery_date.tzinfo is None:
        delivery_date = delivery_date.replace(tzinfo=timezone.utc)

    delivery_doc = {
        "gate_pass_id": primary_gp_id,
        # Denormalised, non-sensitive index field (see _source_filter).
        "source_gate_pass_ids": source_ids,
        "client_name": canonical_client,
        "delivery_date": delivery_date,
        "delivered_by": payload.delivered_by,
        "received_by": payload.received_by,
        "items": planned,
        "status": "DELIVERED",
        "notes": payload.notes,
        "corrections": [],
        "created_at": now,
        "updated_at": now,
    }

    encrypted_delivery = encrypt_dict(delivery_doc, SENSITIVE_FIELDS)
    result = await deliveries_collection.insert_one(encrypted_delivery)
    delivery_id = str(result.inserted_id)

    # ── 5. Re-verify the invariant from current state, roll back on violation ─
    # Two concurrent requests can both pass step 3 against the same snapshot.
    # Re-reading AFTER our write and checking the real invariant — for every
    # (gate pass, item) the confirmed out must not exceed the received in —
    # keeps "never hand back more than was received" true even without
    # transactions. Note we must NOT re-run the pre-flight plan against this
    # snapshot: it already contains our own insert, so replaying the same
    # quantities would double-count them and reject every exact-full delivery.
    check_deliveries, check_returns = await ctx.load_movements(gp_ids)
    check_delivered = ctx.delivered_by_gate_pass(check_deliveries)
    check_returned = ctx.returned_by_gate_pass(check_returns)
    violations = be.find_oversell_violations(
        list(gate_passes.values()),
        check_delivered,
        check_returned,
    )
    if violations:
        await deliveries_collection.delete_one({"_id": result.inserted_id})
        await idempotency.clear(request, auth_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DELIVERY_CONFLICT",
                "message": (
                    "This delivery was rolled back because another delivery "
                    "recorded at the same time used the same balance. Reload the "
                    "available quantities and try again."
                ),
                "errors": violations,
            },
        )

    # ── 6. Derive every affected pass's status from the new quantities ────────
    balances = ctx.balances_for(
        list(gate_passes.values()),
        check_delivered,
        check_returned,
    )
    await ctx.refresh_gate_pass_statuses(source_ids)
    await ctx.sync_gate_passes(source_ids)

    new_version = await bump_version("delivery", result.inserted_id)
    await enqueue_sync("delivery", result.inserted_id, new_version)

    created_doc = await deliveries_collection.find_one({"_id": result.inserted_id})
    serialized = _serialize(created_doc)
    serialized = await attach_verification_to("delivery", result.inserted_id, serialized)

    for gp_id in source_ids:
        balance = balances.get(gp_id, {})
        await record_event(
            entity_type="delivery",
            entity_id=delivery_id,
            event_type=EVENT_DELIVERY_CREATED,
            gate_pass_id=gp_id,
            user_id=auth_id,
            user_name=current_user.get("user_name"),
            item_deltas=[
                build_item_delta(line["item_name"], line.get("specification"), 0, line["quantity"])
                for line in planned
                if str(line.get("gate_pass_id")) == gp_id
            ],
            reason=payload.notes,
            prev_status=gate_passes[gp_id].get("status"),
            new_status=be.derive_gate_pass_status(balance, gate_passes[gp_id].get("status", "")),
            meta={
                "delivery_date": delivery_date.isoformat(),
                "source_gate_pass_ids": source_ids,
                "client_name": canonical_client,
            },
        )

    await log_audit(
        auth_id,
        "DELIVERY_CREATE",
        "delivery",
        delivery_id,
        {"source_gate_pass_ids": source_ids, "client_name": canonical_client},
    )
    await idempotency.record_created(request, auth_id, "delivery", delivery_id)
    return serialized


@router.patch("/{delivery_id}/date", response_model=DeliveryModel)
async def update_delivery_date(
    delivery_id: str,
    payload: DeliveryDateUpdate,
    current_user: dict = Depends(require_capability("delivery:write")),
):
    """Correct the dispatch date on a recorded delivery (special-case correction).

    Allowed regardless of the gate pass status: a date is a record correction,
    not a quantity change, so balances are never affected. A reason is kept for
    the audit trail and the change is journaled as DELIVERY_DATE_CHANGED.
    """
    oid = _parse_object_id(delivery_id, "Delivery record not found")
    doc = await deliveries_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery record not found"
        )

    previous_date = doc.get("delivery_date")
    new_date = payload.delivery_date
    if new_date.tzinfo is None:
        new_date = new_date.replace(tzinfo=timezone.utc)

    await deliveries_collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "delivery_date": new_date,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    updated_doc = await deliveries_collection.find_one({"_id": oid})
    serialized = _serialize(updated_doc)

    new_version = await bump_version("delivery", oid)
    await enqueue_sync("delivery", oid, new_version)
    serialized = await attach_verification_to("delivery", oid, serialized)

    await record_event(
        entity_type="delivery",
        entity_id=serialized["id"],
        event_type=EVENT_DELIVERY_DATE_CHANGED,
        gate_pass_id=serialized.get("gate_pass_id"),
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=payload.reason,
        meta={
            "delivery_date_before": previous_date.isoformat() if previous_date else None,
            "delivery_date_after": new_date.isoformat(),
        },
    )

    await log_audit(
        current_user.get("auth_id", "system"),
        "DELIVERY_DATE_UPDATE",
        "delivery",
        serialized["id"],
    )
    return serialized


@router.patch("/{delivery_id}/items", response_model=DeliveryModel)
async def correct_delivery_items(
    delivery_id: str,
    payload: DeliveryCorrection,
    current_user: dict = Depends(require_capability("delivery:write")),
):
    """Correct recorded delivery quantities, safely and with a full audit trail.

    This is the flow behind the real-world case "we recorded 23, the hotel
    counted 21". It:

      * requires a reason and records who/when/why;
      * keeps the original line quantities in the delivery's ``corrections``
        history and in the immutable event journal — nothing is destroyed;
      * re-validates the corrected quantities against what the source gate
        passes still had available, so a correction can never invent stock;
      * re-derives every affected gate pass's status, which is what turns
        "23 delivered" into "21 delivered, balance 2" everywhere at once.
    """
    oid = _parse_object_id(delivery_id, "Delivery record not found")
    doc = await deliveries_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery record not found"
        )
    if doc.get("status") == be.CANCELLED_STATUS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A cancelled delivery cannot be corrected.",
        )

    try:
        current = _serialize(doc)
    except HTTPException:
        raise
    current_items: List[dict] = list(current.get("items") or [])

    def _match(it: dict) -> tuple:
        return (
            str(it.get("gate_pass_id") or current.get("gate_pass_id") or ""),
            be.item_key(it.get("item_name", ""), it.get("specification")),
        )

    # ── Resolve each correction to exactly one existing line ─────────────────
    changes: List[dict] = []
    consumed: set = set()
    errors: List[dict] = []
    for fix in payload.items:
        spec = fix.specification or None
        gp_id = str(fix.gate_pass_id or current.get("gate_pass_id") or "")
        key = be.item_key(fix.item_name, spec)
        matches = [
            (idx, it)
            for idx, it in enumerate(current_items)
            if _match(it) == (gp_id, key) and (gp_id, key) not in consumed
        ]
        if not matches:
            # Allow addressing a legacy line that carries no own gate_pass_id.
            matches = [
                (idx, it)
                for idx, it in enumerate(current_items)
                if be.item_key(it.get("item_name", ""), it.get("specification")) == key
                and not it.get("gate_pass_id")
                and key not in {c["_key"] for c in changes}
            ]
        if not matches:
            errors.append(
                {
                    "item_name": fix.item_name,
                    "specification": spec or "",
                    "gate_pass_id": gp_id,
                    "detail": "This delivery has no matching line for that item.",
                }
            )
            continue
        idx, original = matches[0]
        consumed.add((gp_id, key))
        changes.append(
            {
                "_index": idx,
                "_key": key,
                "gate_pass_id": gp_id,
                "item_name": original.get("item_name"),
                "specification": original.get("specification") or "",
                "original_quantity": int(original.get("quantity", 0) or 0),
                "corrected_quantity": int(fix.quantity),
                "delta": int(fix.quantity) - int(original.get("quantity", 0) or 0),
            }
        )

    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "CORRECTION_TARGET_NOT_FOUND", "errors": errors},
        )

    unchanged = [c for c in changes if c["delta"] == 0]
    if len(unchanged) == len(changes):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The corrected quantities are identical to the recorded ones.",
        )

    # ── Validate the whole post-correction delivery against availability ─────
    # Availability is measured with THIS delivery excluded, so a correction can
    # claim back the quantity it previously held.
    gp_ids = [str(g) for g in (current.get("source_gate_pass_ids") or [])]
    if not gp_ids:
        gp_ids = [str(current.get("gate_pass_id") or "")]
    gp_ids = [g for g in gp_ids if g]
    if not gp_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Delivery is not linked to any gate pass, so it cannot be validated.",
        )

    gate_passes = {str(gp["gate_pass_id"]): gp for gp in await ctx.load_gate_passes(gp_ids)}
    if not gate_passes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Source gate pass no longer exists"
        )

    deliveries, returns = await ctx.load_movements(gp_ids)
    deliveries = [d for d in deliveries if str(d.get("id")) != str(current.get("id"))]
    available = ctx.availability_maps(
        list(gate_passes.values()),
        ctx.delivered_by_gate_pass(deliveries),
        ctx.returned_by_gate_pass(returns),
    )

    new_items: List[dict] = []
    change_by_index = {c["_index"]: c for c in changes}
    for idx, it in enumerate(current_items):
        change = change_by_index.get(idx)
        if change:
            if change["corrected_quantity"] <= 0:
                # A corrected-to-zero line is dropped from the delivery: the
                # journal keeps the fact it existed and at what quantity.
                continue
            updated = dict(it)
            updated["quantity"] = change["corrected_quantity"]
            updated["discrepancy"] = _recompute_discrepancy(updated)
            new_items.append(updated)
        else:
            new_items.append(it)

    if not new_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A correction cannot remove every line — cancel the delivery instead.",
        )

    try:
        be.plan_delivery_lines(new_items, gate_passes, available)
    except be.DeliveryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "errors": exc.errors},
        )

    # ── Apply, keeping the original values in the record ─────────────────────
    now = datetime.now(timezone.utc)
    correction_record = {
        "corrected_at": now,
        "corrected_by": current_user.get("user_name", ""),
        "corrected_by_id": current_user.get("auth_id", ""),
        "reason": payload.reason,
        "changes": [{k: v for k, v in c.items() if not k.startswith("_")} for c in changes],
    }
    history = list(current.get("corrections") or [])
    history.append(correction_record)

    decrypted_new = dict(current)
    decrypted_new.pop("id", None)
    decrypted_new["items"] = new_items
    decrypted_new["corrections"] = history
    decrypted_new["notes"] = payload.notes if payload.notes is not None else current.get("notes")
    decrypted_new["updated_at"] = now

    # Conditional write. Two operators correcting the same delivery at once
    # would otherwise silently clobber one another: both read the same items,
    # each builds a full document from its own view, and the second replace
    # discards the first correction along with its audit record. Matching on
    # the timestamp we actually read turns that race into a visible 409.
    expected_updated_at = doc.get("updated_at")
    write_filter: dict = {"_id": oid}
    if expected_updated_at is not None:
        write_filter["updated_at"] = expected_updated_at
    else:
        # Legacy rows may predate updated_at; fall back to matching the exact
        # item list we based the correction on.
        write_filter["items"] = doc.get("items")

    result = await deliveries_collection.replace_one(
        write_filter, encrypt_dict(decrypted_new, SENSITIVE_FIELDS)
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DELIVERY_CONFLICT",
                "message": (
                    "This delivery was corrected by someone else while you were "
                    "editing it. Reload the delivery and reapply your correction."
                ),
            },
        )

    # ── Re-check the invariant after writing, and undo if it no longer holds ──
    # Availability was measured from a snapshot taken before the write. Another
    # delivery landing in between can invalidate the correction, so the stored
    # ledger is the authority — exactly as it is for create.
    check_deliveries, check_returns = await ctx.load_movements(gp_ids)
    check_delivered = ctx.delivered_by_gate_pass(check_deliveries)
    check_returned = ctx.returned_by_gate_pass(check_returns)
    violations = be.find_oversell_violations(
        list(gate_passes.values()),
        check_delivered,
        check_returned,
    )
    if violations:
        # `doc` is the untouched original straight from the database, so
        # writing it back restores the exact prior state — encryption included.
        await deliveries_collection.replace_one({"_id": oid}, doc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DELIVERY_CONFLICT",
                "message": (
                    "This correction was rolled back because another delivery "
                    "recorded at the same time used the same balance. Reload the "
                    "delivery and try again."
                ),
                "errors": violations,
            },
        )

    # ── Everything downstream must reflect the new quantities ────────────────
    balances = ctx.balances_for(
        list(gate_passes.values()),
        check_delivered,
        check_returned,
    )
    await ctx.refresh_gate_pass_statuses(gp_ids)
    await ctx.sync_gate_passes(gp_ids)

    new_version = await bump_version("delivery", oid)
    await enqueue_sync("delivery", oid, new_version)

    for gp_id in gp_ids:
        balance = balances.get(gp_id, {})
        await record_event(
            entity_type="delivery",
            entity_id=str(current.get("id")),
            event_type=EVENT_DELIVERY_CORRECTED,
            gate_pass_id=gp_id,
            user_id=current_user.get("auth_id", "system"),
            user_name=current_user.get("user_name"),
            reason=payload.reason,
            item_deltas=[
                build_item_delta(
                    c["item_name"], c["specification"], c["original_quantity"], c["corrected_quantity"]
                )
                for c in changes
                if c["gate_pass_id"] == gp_id
            ],
            prev_status=gate_passes[gp_id].get("status"),
            new_status=be.derive_gate_pass_status(balance, gate_passes[gp_id].get("status", "")),
            meta={
                "resulting_balance": balance["totals"]["outstanding_delivery_qty"],
                "corrected_by": current_user.get("user_name", ""),
            },
        )

    await log_audit(
        current_user.get("auth_id", "system"),
        "DELIVERY_CORRECTION",
        "delivery",
        str(current.get("id")),
        {"reason": payload.reason, "changes": correction_record["changes"]},
    )

    updated = _serialize(await deliveries_collection.find_one({"_id": oid}))
    return await attach_verification_to("delivery", oid, updated)


@router.post("/{delivery_id}/cancel", response_model=DeliveryModel)
async def cancel_delivery(
    delivery_id: str,
    payload: DeliveryCancel,
    current_user: dict = Depends(require_capability("delivery:write")),
):
    """Void a mis-recorded delivery entirely.

    Cancelling releases the whole quantity back to the source gate passes'
    balances, which is derived automatically by the same engine that produced
    them. The document is kept (status CANCELLED) so the history survives, and
    a reason is mandatory so the void can be explained later.
    """
    oid = _parse_object_id(delivery_id, "Delivery record not found")
    doc = await deliveries_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery record not found"
        )
    if doc.get("status") == be.CANCELLED_STATUS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Delivery is already cancelled."
        )

    current = _serialize(doc)
    gp_ids = [
        g
        for g in (current.get("source_gate_pass_ids") or [current.get("gate_pass_id")])
        if g
    ]

    now = datetime.now(timezone.utc)
    await deliveries_collection.update_one(
        {"_id": oid},
        {"$set": {"status": "CANCELLED", "cancelled_at": now, "updated_at": now,
                  "cancelled_by": current_user.get("user_name", ""),
                  "cancelled_by_id": current_user.get("auth_id", ""),
                  "cancelled_reason": payload.reason}},
    )

    await ctx.refresh_gate_pass_statuses(gp_ids)
    await ctx.sync_gate_passes(gp_ids)
    new_version = await bump_version("delivery", oid)
    await enqueue_sync("delivery", oid, new_version)

    for gp_id in gp_ids:
        await record_event(
            entity_type="delivery",
            entity_id=str(current.get("id")),
            event_type=EVENT_DELIVERY_CANCELLED,
            gate_pass_id=gp_id,
            user_id=current_user.get("auth_id", "system"),
            user_name=current_user.get("user_name"),
            reason=payload.reason,
            item_deltas=[
                build_item_delta(
                    it.get("item_name", ""), it.get("specification"), int(it.get("quantity", 0) or 0), 0
                )
                for it in current.get("items", [])
                if str(it.get("gate_pass_id") or current.get("gate_pass_id")) == str(gp_id)
            ],
            meta={
                "cancelled_by": current_user.get("user_name", ""),
                "released_quantity": sum(
                    int(it.get("quantity", 0) or 0)
                    for it in current.get("items", [])
                    if str(it.get("gate_pass_id") or current.get("gate_pass_id")) == str(gp_id)
                ),
            },
        )

    await log_audit(
        current_user.get("auth_id", "system"),
        "DELIVERY_CANCEL",
        "delivery",
        str(current.get("id")),
        {"reason": payload.reason},
    )
    updated = _serialize(await deliveries_collection.find_one({"_id": oid}))
    return await attach_verification_to("delivery", oid, updated)


@router.get("/available")
async def available_quantities(
    client_name: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("delivery:read")),
):
    """What can be delivered right now, per hotel and per source gate pass.

    The delivery form, the gate-pass detail page and the dashboard all render
    this, so an administrator never subtracts quantities by hand. Each row
    states its origin (gate pass number + receiving date), what was received,
    what has already been delivered, and the exact remaining quantity.
    """
    from ..crypto_helper import get_search_token

    query: dict = {"status": {"$ne": be.CANCELLED_STATUS}}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)

    gate_passes = await ctx.load_gate_passes()
    gate_passes = [
        gp
        for gp in gate_passes
        if not client_name or be.same_client(gp.get("client_name"), client_name)
    ]
    gp_ids = [str(gp["gate_pass_id"]) for gp in gate_passes]
    deliveries, returns = await ctx.load_movements(gp_ids)

    rows = be.build_availability(
        gate_passes,
        ctx.delivered_by_gate_pass(deliveries),
        ctx.returned_by_gate_pass(returns),
    )
    deliverable = [r for r in rows if r["total_available_qty"] > 0]
    return {
        "client_name": client_name,
        "gate_passes": deliverable,
        "total_available_qty": sum(r["total_available_qty"] for r in deliverable),
    }


@router.get("/pending-gatepasses")
async def pending_gatepasses(
    client_name: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("delivery:read")),
):
    """Gate passes that still have items to deliver, grouped by hotel.

    Kept for the multi-select delivery form. Sourced from the same engine as
    ``/deliveries/available`` so the number a user is allowed to type always
    matches the number they are shown.
    """
    gate_passes = await ctx.load_gate_passes()
    gate_passes = [
        gp
        for gp in gate_passes
        if gp.get("status") != be.CANCELLED_STATUS
        and (not client_name or be.same_client(gp.get("client_name"), client_name))
    ]
    gp_ids = [str(gp["gate_pass_id"]) for gp in gate_passes]
    deliveries, returns = await ctx.load_movements(gp_ids)

    rows = be.build_availability(
        gate_passes,
        ctx.delivered_by_gate_pass(deliveries),
        ctx.returned_by_gate_pass(returns),
    )

    results = []
    for row in rows:
        items_with_pending = [
            {
                "item_name": ln["item_name"],
                "specification": ln["specification"],
                "category": ln["category"],
                "received_qty": ln["received_qty"],
                "delivered_qty": ln["delivered_qty"],
                "returned_qty": ln["returned_qty"],
                "pending_qty": ln["available_qty"],
            }
            for ln in row["deliverable_items"]
        ]
        if not items_with_pending:
            continue
        results.append(
            {
                "gate_pass_id": row["gate_pass_id"],
                "gate_pass_number": row["gate_pass_number"],
                "client_name": row["client_name"],
                "receiving_date": str(row["receiving_date"] or "")[:10],
                "status": row["status"],
                "total_pending": sum(i["pending_qty"] for i in items_with_pending),
                "items": items_with_pending,
            }
        )
    return results


@router.get("/{delivery_id}/balance")
async def delivery_balance(
    delivery_id: str,
    current_user: dict = Depends(require_capability("delivery:read")),
):
    """A delivery plus the resulting balance of every gate pass it touched.

    Answers the questions an administrator asks when reviewing a delivery:
    what was its original recorded quantity, what was it corrected to, who
    corrected it and why, and what now remains on each source gate pass.
    """
    oid = _parse_object_id(delivery_id, "Delivery record not found")
    doc = await deliveries_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery record not found"
        )
    current = _serialize(doc)
    gp_ids = [
        str(g)
        for g in (current.get("source_gate_pass_ids") or [current.get("gate_pass_id")])
        if g
    ]
    gate_passes = await ctx.load_gate_passes(gp_ids)
    gp_map = {str(gp["gate_pass_id"]): gp for gp in gate_passes}
    deliveries, returns = await ctx.load_movements(gp_ids)
    balances = ctx.balances_for(
        gate_passes,
        ctx.delivered_by_gate_pass(deliveries),
        ctx.returned_by_gate_pass(returns),
    )

    per_gate_pass = []
    for gp_id in gp_ids:
        gp = gp_map.get(gp_id)
        if not gp:
            continue
        balance = balances.get(gp_id)
        if not balance:
            continue
        per_gate_pass.append(
            {
                "gate_pass_id": gp_id,
                "gate_pass_number": gp.get("gate_pass_number"),
                "client_name": gp.get("client_name"),
                "receiving_date": gp.get("receiving_date"),
                "status": gp.get("status"),
                "derived_status": be.derive_gate_pass_status(balance, gp.get("status", "")),
                "items": list(balance["items"].values()),
                "totals": balance["totals"],
                "flags": balance["flags"],
            }
        )

    return {
        "delivery": current,
        "original_items": current.get("items") or [],
        "corrections": current.get("corrections") or [],
        "source_gate_passes": per_gate_pass,
    }


@router.get("", response_model=List[DeliveryModel])
async def list_deliveries(
    client_name: Optional[str] = Query(None),
    gate_pass_id: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    include_cancelled: bool = Query(False),
    current_user: dict = Depends(require_capability("delivery:read")),
):
    query: dict = _source_filter(gate_pass_id)

    if client_name:
        query["client_name_search"] = get_search_token(client_name)

    if not include_cancelled:
        query["status"] = {"$ne": "CANCELLED"}

    if date_from or date_to:
        date_query = {}
        if date_from:
            date_query["$gte"] = date_from if date_from.tzinfo else date_from.replace(tzinfo=timezone.utc)
        if date_to:
            date_query["$lte"] = date_to if date_to.tzinfo else date_to.replace(tzinfo=timezone.utc)
        query["delivery_date"] = date_query

    cursor = deliveries_collection.find(query).sort("delivery_date", -1)
    rows: List[dict] = []
    all_source_ids: List[str] = []
    for doc in await cursor.to_list(length=None):
        try:
            serialized = _serialize(doc)
            rows.append(await attach_verification_to("delivery", doc["_id"], serialized))
            for gp_id in be.source_gate_pass_ids(serialized):
                if gp_id not in all_source_ids:
                    all_source_ids.append(gp_id)
        except HTTPException:
            pass

    # Attach each source gate pass's canonical balance totals to every row.
    #
    # The list used to be rendered with a progress bar the browser computed as
    # `delivered / received` against ONE gate pass, which is meaningless for a
    # delivery spanning several. Doing it here means the list shows the same
    # numbers as the gate-pass detail screen and the delivery form.
    if all_source_ids:
        gps = await ctx.load_gate_passes(all_source_ids)
        movements, returns = await ctx.load_movements(all_source_ids)
        balances = ctx.balances_for(
            gps,
            ctx.delivered_by_gate_pass(movements),
            ctx.returned_by_gate_pass(returns),
        )
        summaries: Dict[str, dict] = {}
        for gp in gps:
            gp_id = str(gp["gate_pass_id"])
            balance = balances.get(gp_id)
            if not balance:
                continue
            summaries[gp_id] = {
                "gate_pass_id": gp_id,
                "gate_pass_number": gp.get("gate_pass_number"),
                "client_name": gp.get("client_name"),
                "status": gp.get("status"),
                "derived_status": be.derive_gate_pass_status(balance, gp.get("status", "")),
                "totals": balance["totals"],
            }
        for row in rows:
            row["source_gate_passes"] = [
                summaries[g] for g in be.source_gate_pass_ids(row) if g in summaries
            ]

    return rows


@router.get("/{delivery_id}", response_model=DeliveryModel)
async def get_delivery(
    delivery_id: str,
    current_user: dict = Depends(require_capability("delivery:read")),
):
    oid = _parse_object_id(delivery_id, "Delivery record not found")
    doc = await deliveries_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery record not found"
        )
    serialized = _serialize(doc)
    return await attach_verification_to("delivery", oid, serialized)

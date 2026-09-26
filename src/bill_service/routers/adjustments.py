"""Controlled adjustment workflow.

Users must NEVER rewrite a gate pass's received quantities directly once
movement exists. Instead they create an adjustment request which, only
after a second user approves it, changes the balance. The original event
is preserved in the event journal; the source documents are themselves
never edited in place.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict
from ..database.main_db import (
    adjustments_collection,
    deliveries_collection,
    gatepasses_collection,
    returns_collection,
)
from ..models import GatePassAdjustmentRequest
from ..router_utils import parse_object_id
from ..services import balance_engine as be
from ..services.bill_sync import sync_bills_to_gate_pass
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_ADJUSTMENT_APPROVED,
    EVENT_ADJUSTMENT_REJECTED,
    EVENT_ADJUSTMENT_REQUESTED,
)

router = APIRouter(prefix="/adjustments", tags=["adjustments"])

SENSITIVE_FIELDS = ["client_name", "items", "notes"]


async def _get_open_gp(gate_pass_id: str):
    """Fetch and decrypt a non-cancelled gate pass."""
    oid = parse_object_id(gate_pass_id, "gate pass ID")
    gp_doc = await gatepasses_collection.find_one({"_id": oid})
    if not gp_doc:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    if gp_doc.get("status") == "CANCELLED":
        raise HTTPException(
            status_code=409, detail="Cannot adjust a cancelled gate pass."
        )
    return oid, decrypt_dict(gp_doc, SENSITIVE_FIELDS)


async def create_adjustment_request(
    payload: GatePassAdjustmentRequest,
    current_user: dict,
) -> dict:
    """Create an adjustment request. Does NOT change any quantity."""
    gp_oid, decrypted = await _get_open_gp(payload.gate_pass_id)

    original_qty = None
    for it in decrypted.get("items", []):
        if (
            it.get("item_name") == payload.item_name
            and (it.get("specification") or "") == (payload.specification or "")
        ):
            original_qty = int(it.get("received_qty", 0) or 0)
            break
    if original_qty is None:
        raise HTTPException(
            status_code=404,
            detail=f"Item '{payload.item_name}' not found on the gate pass.",
        )

    now = datetime.now(timezone.utc)
    adj_doc = {
        "gate_pass_id": str(gp_oid),
        "item_name": payload.item_name,
        "specification": payload.specification or "",
        "original_qty": original_qty,
        "corrected_qty": payload.corrected_qty,
        "reason": payload.reason,
        "status": "REQUESTED",
        "requested_by": current_user.get("user_name", ""),
        "requested_by_id": current_user.get("auth_id", ""),
        "created_at": now,
        "updated_at": now,
    }
    result = await adjustments_collection.insert_one(adj_doc)
    adj_doc["id"] = str(result.inserted_id)

    await record_event(
        entity_type="adjustment",
        entity_id=adj_doc["id"],
        event_type=EVENT_ADJUSTMENT_REQUESTED,
        gate_pass_id=str(gp_oid),
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=payload.reason,
        item_deltas=[
            build_item_delta(payload.item_name, payload.specification, original_qty, payload.corrected_qty)
        ],
        meta={"status": "REQUESTED"},
    )
    return adj_doc


@router.post("")
async def create_adjustment_route(
    payload: GatePassAdjustmentRequest,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """POST /adjustments — stage an adjustment request (no quantity change)."""
    return await create_adjustment_request(payload, current_user)


@router.get("")
async def list_adjustments(
    gate_pass_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    query: dict = {}
    if gate_pass_id:
        query["gate_pass_id"] = gate_pass_id
    if status:
        query["status"] = status
    cursor = adjustments_collection.find(query).sort("created_at", -1)
    out = []
    async for doc in cursor:
        doc["id"] = str(doc["_id"])
        del doc["_id"]
        out.append(doc)
    return out


async def _get_adjustment(adj_id: str):
    oid = parse_object_id(adj_id, "adjustment ID")
    doc = await adjustments_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Adjustment not found")
    return oid, doc


@router.post("/{adjustment_id}/approve")
async def approve_adjustment(
    adjustment_id: str,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Approve and apply an adjustment. Must be a different user than the requester."""
    oid, adj_doc = await _get_adjustment(adjustment_id)

    if adj_doc.get("status") != "REQUESTED":
        raise HTTPException(
            status_code=409,
            detail=f"Adjustment is already {adj_doc.get('status')}.",
        )
    requester_id = str(adj_doc.get("requested_by_id") or "").strip()
    approver_id = str(current_user.get("auth_id") or "").strip()
    if requester_id and approver_id and requester_id == approver_id:
        raise HTTPException(
            status_code=400,
            detail="Adjustments must be approved by a different user than the requester.",
        )

    # Claim the request BEFORE changing anything.
    #
    # The status was only read above, so two supervisors pressing Approve at the
    # same time both saw REQUESTED, both rewrote the gate pass, and both appended
    # to the history -- the correction was applied twice. A conditional update
    # lets exactly one of them win; the loser is told it is already approved.
    now = datetime.now(timezone.utc)
    claimed = await adjustments_collection.find_one_and_update(
        {"_id": oid, "status": "REQUESTED"},
        {
            "$set": {
                "status": "APPROVING",
                "approving_started_at": now,
                "updated_at": now,
            }
        },
    )
    if claimed is None:
        raise HTTPException(
            status_code=409,
            detail="This adjustment is already being approved by someone else.",
        )

    try:
        gp_oid, gp_dec, updated_items, original_qty, history = await _apply_approved_correction(
            oid, adj_doc, current_user
        )
    except HTTPException:
        # Nothing was written, so hand the request back for someone else to try.
        await adjustments_collection.update_one(
            {"_id": oid, "status": "APPROVING"},
            {"$set": {"status": "REQUESTED", "updated_at": datetime.now(timezone.utc)}},
        )
        raise

    encrypted_gp = encrypt_dict(gp_dec, SENSITIVE_FIELDS)
    await gatepasses_collection.replace_one({"_id": gp_oid}, encrypted_gp)

    # Automatic propagation: any linked, still-editable bill is re-clamped to
    # the corrected received quantities; paid bills are flagged, never rewritten.
    #
    # A failure here used to be logged and swallowed, and the response still
    # said APPROVED -- so the correction was on the gate pass, the request was
    # closed, and the bills silently disagreed with it. The outcome is now
    # recorded on the request and reported to the caller.
    bill_sync = "OK"
    try:
        await sync_bills_to_gate_pass(
            str(gp_oid),
            updated_items,
            user_id=current_user.get("auth_id", "system"),
            user_name=current_user.get("user_name"),
            reason=adj_doc.get("reason"),
        )
    except Exception:
        import logging

        bill_sync = "FAILED"
        logging.getLogger("bill_service").exception(
            "bill_sync failed after adjustment approval %s", oid
        )

    await adjustments_collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "APPROVED",
                "approved_by": current_user.get("user_name", ""),
                "approved_by_id": current_user.get("auth_id", ""),
                "approved_at": now,
                "bill_sync": bill_sync,
                "updated_at": now,
            }
        },
    )

    await record_event(
        entity_type="adjustment",
        entity_id=str(oid),
        event_type=EVENT_ADJUSTMENT_APPROVED,
        gate_pass_id=str(gp_oid),
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=adj_doc.get("reason"),
        item_deltas=[
            build_item_delta(
                adj_doc["item_name"],
                adj_doc.get("specification"),
                original_qty,
                int(adj_doc.get("corrected_qty", 0) or 0),
            )
        ],
        meta={
            "approved_by": current_user.get("user_name", ""),
            "bill_sync": bill_sync,
        },
    )

    response = {"id": str(oid), "status": "APPROVED", "gate_pass_id": str(gp_oid)}
    if bill_sync == "FAILED":
        response["bill_sync"] = (
            "FAILED: the correction is recorded, but the linked bill could not be "
            "re-calculated. Re-run bill sync for this gate pass before invoicing."
        )
    return response


async def _apply_approved_correction(oid, adj_doc, current_user):
    """Build the corrected gate pass. Raises before any write if it no longer applies."""
    gp_oid, gp_dec = await _get_open_gp(adj_doc["gate_pass_id"])
    updated_items, original_qty = be.apply_received_correction(
        gp_dec.get("items", []),
        adj_doc["item_name"],
        adj_doc.get("specification"),
        int(adj_doc.get("corrected_qty", 0) or 0),
    )
    if updated_items is None:
        raise HTTPException(
            status_code=409,
            detail="Item no longer exists on the gate pass — request is stale.",
        )

    now = datetime.now(timezone.utc)
    history = gp_dec.get("adjustments", []) or []
    history.append(
        {
            "adjustment_id": str(oid),
            "item_name": adj_doc["item_name"],
            "specification": adj_doc.get("specification") or "",
            "original_value": original_qty,
            "corrected_value": int(adj_doc.get("corrected_qty", 0) or 0),
            "reason": adj_doc.get("reason"),
            "requested_by": adj_doc.get("requested_by", ""),
            "approved_by": current_user.get("user_name", ""),
            "timestamp": now,
        }
    )

    new_gp = dict(gp_dec)
    new_gp["items"] = updated_items
    new_gp["adjustments"] = history
    new_gp["updated_at"] = now

    # Re-derive gate pass status from the corrected quantities INCLUDING all
    # real movements — ignoring recorded deliveries/returns downgraded a fully
    # delivered pass (e.g. 50 received, 50 delivered, corrected to 47) to
    # PARTIALLY_DELIVERED/RECEIVED.
    delivered_docs = []
    async for dl in deliveries_collection.find(
        {"gate_pass_id": str(gp_oid), "status": {"$ne": "CANCELLED"}}
    ):
        delivered_docs.append(dl)
    return_docs = []
    async for rt in returns_collection.find({"gate_pass_id": str(gp_oid)}):
        return_docs.append(rt)

    new_gp["status"] = be.recompute_status_with_movements(
        updated_items,
        [decrypt_dict(x, SENSITIVE_FIELDS) for x in delivered_docs],
        [decrypt_dict(x, SENSITIVE_FIELDS) for x in return_docs],
        new_gp.get("status", "RECEIVED"),
    )

    return gp_oid, new_gp, updated_items, original_qty, history

@router.post("/{adjustment_id}/reject")
async def reject_adjustment(
    adjustment_id: str,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    oid, adj_doc = await _get_adjustment(adjustment_id)
    if adj_doc.get("status") != "REQUESTED":
        raise HTTPException(
            status_code=409,
            detail=f"Adjustment is already {adj_doc.get('status')}.",
        )
    now = datetime.now(timezone.utc)
    # Conditional, for the same reason approval is: a reject that lost the race
    # to an approval must not overwrite the approved outcome.
    result = await adjustments_collection.update_one(
        {"_id": oid, "status": "REQUESTED"},
        {
            "$set": {
                "status": "REJECTED",
                "rejected_by": current_user.get("user_name", ""),
                "rejected_at": now,
                "updated_at": now,
            }
        },
    )
    if result.modified_count == 0:
        raise HTTPException(
            status_code=409,
            detail="This adjustment is no longer awaiting a decision.",
        )

    await record_event(
        entity_type="adjustment",
        entity_id=str(oid),
        event_type=EVENT_ADJUSTMENT_REJECTED,
        gate_pass_id=adj_doc.get("gate_pass_id"),
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=adj_doc.get("reason"),
        meta={"rejected_by": current_user.get("user_name", "")},
    )
    return {"id": str(oid), "status": "REJECTED", "gate_pass_id": adj_doc.get("gate_pass_id")}
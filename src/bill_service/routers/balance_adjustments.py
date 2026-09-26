"""Signed balance adjustments and the delivery balance report.

A delivery is sometimes recorded with the wrong quantity, or pieces go missing
in transit. Rather than rewriting history, a balance adjustment is posted
against one item of one gate pass:

    +3   we under-delivered / lost / damaged  -> the client is owed 3 more
    -3   we over-recorded the send            -> 3 fewer are outstanding

These apply immediately (no second approver) because they are corrections, not
approvals — but a reason is mandatory and every post is journalled and audited.

Money is deliberately untouched. Billing derives from the gate pass received
quantity, so posting an adjustment can never move an invoice. The adjustment
only moves the PIECE balance, which is what the delivery note prints.
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..auth_helper import require_capability
from ..crypto_helper import decrypt_dict
from ..database.main_db import (
    audit_collection,
    balance_adjustments_collection,
    deliveries_collection,
    gatepasses_collection,
    returns_collection,
)
from ..models import BalanceAdjustmentCreate, BalanceAdjustmentModel, DeliveryBalanceReport
from ..router_utils import log_audit, parse_object_id
from ..services import balance_engine as be
from ..services import idempotency
from ..services.transaction_events import (
    EVENT_BALANCE_ADJUSTMENT_POSTED,
    EVENT_BALANCE_ADJUSTMENT_VOIDED,
    build_item_delta,
    record_event,
)

router = APIRouter(tags=["balance-adjustments"])

GP_SENSITIVE_FIELDS = ["client_name", "items", "notes"]
DELIVERY_SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def _serialize_adjustment(doc: dict) -> dict:
    doc["id"] = str(doc["_id"])
    del doc["_id"]
    return doc


async def _load_gate_pass(gate_pass_id: str):
    """Fetch and decrypt a non-cancelled gate pass."""
    oid = parse_object_id(gate_pass_id, "gate pass ID")
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    if doc.get("status") == "CANCELLED":
        raise HTTPException(
            status_code=409, detail="Cannot adjust the balance of a cancelled gate pass."
        )
    return oid, decrypt_dict(doc, GP_SENSITIVE_FIELDS)


async def _gate_pass_context(gate_pass_id: str):
    """Load a gate pass plus its delivered / returned / adjusted item maps.

    Returns ``(gp_oid, gp_decrypted, delivered_by_item, returned_by_item,
    balance_adjustment_by_item, adjustment_docs, deliveries)`` so no caller has
    to re-implement the aggregation. ``deliveries`` is the decrypted list
    itself, which the print report needs in order to establish sequence.
    """
    gp_oid, gp_dec = await _load_gate_pass(gate_pass_id)

    deliveries: List[dict] = []
    async for dl in deliveries_collection.find(
        {"gate_pass_id": gate_pass_id, "status": {"$ne": "CANCELLED"}}
    ):
        try:
            deliveries.append(decrypt_dict(dl, DELIVERY_SENSITIVE_FIELDS))
        except Exception:
            continue

    returns: List[dict] = []
    async for rt in returns_collection.find({"gate_pass_id": gate_pass_id}):
        try:
            returns.append(decrypt_dict(rt, GP_SENSITIVE_FIELDS))
        except Exception:
            continue

    adjustment_docs: List[dict] = []
    async for adj in balance_adjustments_collection.find({"gate_pass_id": gate_pass_id}):
        adjustment_docs.append(adj)

    return (
        gp_oid,
        gp_dec,
        be.compute_delivered_by_item(deliveries),
        be.compute_returned_by_item(returns),
        be.compute_balance_adjustments_by_item(adjustment_docs),
        adjustment_docs,
        deliveries,
    )


@router.post(
    "/balance-adjustments",
    response_model=BalanceAdjustmentModel,
    status_code=status.HTTP_201_CREATED,
)
async def post_balance_adjustment(
    payload: BalanceAdjustmentCreate,
    request: Request = None,
    current_user: dict = Depends(require_capability("delivery:write")),
):
    """POST /balance-adjustments — post a signed correction to an item balance.

    Applies immediately; a reason is mandatory. The gate pass status is
    re-derived so a pass that becomes owed to the client stops reading
    DELIVERED. Billing is intentionally NOT re-clamped: the correction is a
    piece count and money comes from received_qty alone.
    """
    auth_id = current_user.get("auth_id", "system")

    existing = await idempotency.find_previous(
        request, auth_id, balance_adjustments_collection
    )
    if existing:
        return _serialize_adjustment(existing)

    (
        gp_oid,
        gp_dec,
        delivered_by_item,
        returned_by_item,
        _,
        adjustment_docs,
        _deliveries,
    ) = await _gate_pass_context(payload.gate_pass_id)

    # The item must actually exist on the gate pass, otherwise the correction
    # would sit against nothing and silently never show up on a balance.
    target_key = be.item_key(payload.item_name, payload.specification)
    if not any(
        be.item_key(it.get("item_name", ""), it.get("specification")) == target_key
        for it in gp_dec.get("items", [])
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Item '{payload.item_name}'"
                + (f" ({payload.specification})" if payload.specification else "")
                + " was not received on this gate pass."
            ),
        )

    delivery_id = payload.delivery_id
    if delivery_id:
        parse_object_id(delivery_id, "delivery ID")
        dl = await deliveries_collection.find_one({"_id": parse_object_id(delivery_id, "delivery ID")})
        if not dl:
            raise HTTPException(status_code=404, detail="Delivery record not found")
        if dl.get("gate_pass_id") != payload.gate_pass_id:
            raise HTTPException(
                status_code=400,
                detail="That delivery belongs to a different gate pass.",
            )

    before = be.compute_gate_pass_balance(
        gp_dec.get("items", []),
        delivered_by_item,
        returned_by_item,
        balance_adjustment_by_item=be.compute_balance_adjustments_by_item(adjustment_docs),
    )["items"].get(target_key, {}).get("outstanding_delivery_qty", 0)

    now = datetime.now(timezone.utc)
    adj_doc = {
        "gate_pass_id": payload.gate_pass_id,
        "delivery_id": delivery_id,
        "item_name": payload.item_name,
        "specification": payload.specification or "",
        "quantity": payload.quantity,
        "reason": payload.reason,
        "notes": payload.notes,
        "status": "POSTED",
        "created_by": current_user.get("user_name", ""),
        "created_by_id": auth_id,
        "created_at": now,
    }
    result = await balance_adjustments_collection.insert_one(adj_doc)
    created = await balance_adjustments_collection.find_one({"_id": result.inserted_id})

    # Re-derive the status from the corrected quantities INCLUDING movements, so
    # crediting a client on a fully-delivered pass flips it back to
    # PARTIALLY_DELIVERED instead of leaving a false DELIVERED.
    balance_after = be.compute_gate_pass_balance(
        gp_dec.get("items", []),
        delivered_by_item,
        returned_by_item,
        balance_adjustment_by_item=be.compute_balance_adjustments_by_item(
            adjustment_docs + [adj_doc]
        ),
    )
    new_status = be.derive_gate_pass_status(
        balance_after, gp_dec.get("status", "RECEIVED")
    )
    after = balance_after["items"].get(target_key, {}).get("outstanding_delivery_qty", 0)

    from ..repositories.main_repository import bump_version, enqueue_sync

    if new_status != gp_dec.get("status"):
        await gatepasses_collection.update_one(
            {"_id": gp_oid}, {"$set": {"status": new_status, "updated_at": now}}
        )
        gp_version = await bump_version("gatepass", gp_oid)
        await enqueue_sync("gatepass", gp_oid, gp_version)

    await record_event(
        entity_type="balance_adjustment",
        entity_id=str(result.inserted_id),
        event_type=EVENT_BALANCE_ADJUSTMENT_POSTED,
        gate_pass_id=payload.gate_pass_id,
        user_id=auth_id,
        user_name=current_user.get("user_name"),
        reason=payload.reason,
        item_deltas=[
            build_item_delta(payload.item_name, payload.specification, before, after)
        ],
        meta={
            "delivery_id": delivery_id,
            "signed_quantity": payload.quantity,
            "outstanding_before": before,
            "outstanding_after": after,
            "affects_billing": False,
        },
    )

    await log_audit(
        auth_id,
        "BALANCE_ADJUSTMENT_POST",
        "balance_adjustment",
        str(result.inserted_id),
        {
            "gate_pass_id": payload.gate_pass_id,
            "delivery_id": delivery_id,
            "item_name": payload.item_name,
            "specification": payload.specification or "",
            "quantity": payload.quantity,
            "reason": payload.reason,
            "outstanding_before": before,
            "outstanding_after": after,
        },
        audit_collection,
    )
    await idempotency.record_created(
        request, auth_id, "balance_adjustment", str(result.inserted_id)
    )
    return _serialize_adjustment(created)


@router.get("/balance-adjustments", response_model=List[BalanceAdjustmentModel])
async def list_balance_adjustments(
    gate_pass_id: Optional[str] = Query(None),
    delivery_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: dict = Depends(require_capability("delivery:read")),
):
    """GET /balance-adjustments — newest first, optionally scoped."""
    query: dict = {}
    if gate_pass_id:
        query["gate_pass_id"] = gate_pass_id
    if delivery_id:
        query["delivery_id"] = delivery_id
    if status_filter:
        query["status"] = status_filter

    out: List[dict] = []
    async for doc in balance_adjustments_collection.find(query).sort("created_at", -1):
        out.append(_serialize_adjustment(dict(doc)))
    return out


@router.post("/balance-adjustments/{adjustment_id}/void")
async def void_balance_adjustment(
    adjustment_id: str,
    reason: str = Query(..., min_length=1),
    current_user: dict = Depends(require_capability("delivery:write")),
):
    """Void a posted adjustment. Nothing is deleted — it is marked VOID.

    Voiding is the reversal path for a correction that was itself wrong. The
    engine ignores VOID documents, so the balance returns to its prior figure.
    """
    oid = parse_object_id(adjustment_id, "adjustment ID")
    doc = await balance_adjustments_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Balance adjustment not found")
    if doc.get("status") == "VOID":
        raise HTTPException(status_code=409, detail="Adjustment is already voided.")

    # Voiding reverses the balance, so the pass status has to be re-derived too:
    # crediting a client reopened a closed pass, and voiding that credit must
    # close it again. Without this the pass stays stuck on the status the
    # correction produced.
    (
        gp_oid,
        gp_dec,
        delivered_by_item,
        returned_by_item,
        _,
        adjustment_docs,
        _deliveries,
    ) = await _gate_pass_context(doc.get("gate_pass_id") or "")

    target_key = be.item_key(doc.get("item_name", ""), doc.get("specification"))
    before = (
        be.compute_gate_pass_balance(
            gp_dec.get("items", []),
            delivered_by_item,
            returned_by_item,
            balance_adjustment_by_item=be.compute_balance_adjustments_by_item(
                adjustment_docs
            ),
        )["items"]
        .get(target_key, {})
        .get("outstanding_delivery_qty", 0)
    )

    now = datetime.now(timezone.utc)
    await balance_adjustments_collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "VOID",
                "voided_at": now,
                "voided_by": current_user.get("user_name", ""),
                "void_reason": reason,
            }
        },
    )

    remaining = [a for a in adjustment_docs if str(a.get("_id")) != str(oid)]
    balance_after = be.compute_gate_pass_balance(
        gp_dec.get("items", []),
        delivered_by_item,
        returned_by_item,
        balance_adjustment_by_item=be.compute_balance_adjustments_by_item(remaining),
    )
    after = balance_after["items"].get(target_key, {}).get("outstanding_delivery_qty", 0)
    new_status = be.derive_gate_pass_status(
        balance_after, gp_dec.get("status", "RECEIVED")
    )

    from ..repositories.main_repository import bump_version, enqueue_sync

    if new_status != gp_dec.get("status"):
        await gatepasses_collection.update_one(
            {"_id": gp_oid}, {"$set": {"status": new_status, "updated_at": now}}
        )
        gp_version = await bump_version("gatepass", gp_oid)
        await enqueue_sync("gatepass", gp_oid, gp_version)

    await record_event(
        entity_type="balance_adjustment",
        entity_id=adjustment_id,
        event_type=EVENT_BALANCE_ADJUSTMENT_VOIDED,
        gate_pass_id=doc.get("gate_pass_id"),
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=reason,
        item_deltas=[
            build_item_delta(
                doc.get("item_name", ""), doc.get("specification"), before, after
            )
        ],
        meta={
            "voided_signed_quantity": doc.get("quantity"),
            "outstanding_before": before,
            "outstanding_after": after,
            "gate_pass_status_after": new_status,
            "affects_billing": False,
        },
    )
    await log_audit(
        current_user.get("auth_id", "system"),
        "BALANCE_ADJUSTMENT_VOID",
        "balance_adjustment",
        adjustment_id,
        {
            "reason": reason,
            "item_name": doc.get("item_name", ""),
            "specification": doc.get("specification") or "",
            "quantity": doc.get("quantity"),
            "outstanding_before": before,
            "outstanding_after": after,
            "gate_pass_status_after": new_status,
        },
        audit_collection,
    )
    return {"id": adjustment_id, "status": "VOID"}


@router.get("/deliveries/{delivery_id}/balance-report", response_model=DeliveryBalanceReport)
async def delivery_balance_report(
    delivery_id: str,
    current_user: dict = Depends(require_capability("delivery:read")),
):
    """GET /deliveries/{id}/balance-report — the printed running balance.

    Feeds the delivery note: previous balance, received, delivered, any
    correction and the resulting current balance, per item.

    The figures are evaluated as of the END of this delivery, so a note printed
    today does not change because the client is served again tomorrow. For the
    most recent delivery that as-of figure is the gate-pass outstanding balance,
    so the note and the balance screen agree where it matters.
    """
    oid = parse_object_id(delivery_id, "delivery ID")
    dl_doc = await deliveries_collection.find_one({"_id": oid})
    if not dl_doc:
        raise HTTPException(status_code=404, detail="Delivery record not found")
    if dl_doc.get("status") == "CANCELLED":
        raise HTTPException(
            status_code=409, detail="Cannot report the balance of a cancelled delivery."
        )

    delivery = decrypt_dict(dl_doc, DELIVERY_SENSITIVE_FIELDS)
    gate_pass_id = delivery.get("gate_pass_id") or ""
    (
        _,
        gp_dec,
        _delivered_by_item,
        returned_by_item,
        _balance_adjustment_by_item,
        adjustment_docs,
        all_deliveries,
    ) = await _gate_pass_context(gate_pass_id)

    # The note is a statement about the running sequence as of THIS delivery,
    # so later deliveries and the corrections attached to them are excluded.
    ordered = be.order_deliveries(all_deliveries, delivery_id)

    report = be.compute_delivery_balance_report(
        gp_dec.get("items", []),
        delivery,
        ordered,
        returned_by_item,
        adjustment_docs,
    )

    return {
        "delivery_id": delivery_id,
        "gate_pass_id": gate_pass_id,
        "gate_pass_number": gp_dec.get("gate_pass_number"),
        "client_name": delivery.get("client_name"),
        "delivery_date": delivery.get("delivery_date"),
        "items": report["items"],
        "totals": report["totals"],
        "flags": report["flags"],
    }

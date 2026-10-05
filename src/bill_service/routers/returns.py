"""Returns router — record garment returns from clients."""
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import returns_collection, gatepasses_collection, deliveries_collection
from ..models import ReturnCreate, ReturnUpdate, RETURN_STATUSES
from ..router_utils import parse_object_id, log_audit
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_RETURN_CREATED,
    EVENT_RETURN_RESENT,
    EVENT_RETURN_UPDATED,
)
from ..crypto_helper import get_search_token, encrypt_dict, decrypt_dict
from ..services import balance_engine as be
from ..services import operations_context as ops

router = APIRouter(tags=["Returns"])

SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def _dec(doc: dict) -> dict:
    """Decrypt and convert _id to id. Handles unencrypted legacy docs gracefully."""
    try:
        decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    except (ValueError, KeyError):
        decrypted = {k: v for k, v in doc.items() if k != "encryption_metadata" and not k.endswith("_search")}
    if "_id" in decrypted:
        decrypted["id"] = str(decrypted["_id"])
        del decrypted["_id"]
    return decrypted


def _enc(doc: dict) -> dict:
    """Encrypt sensitive fields for storage. Strips id field."""
    to_encrypt = {k: v for k, v in doc.items() if k != "id" and k != "_id"}
    return encrypt_dict(to_encrypt, SENSITIVE_FIELDS)


def _generate_return_id() -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"RT-{code}"


def _decrypted(doc: dict) -> dict:
    try:
        return decrypt_dict(doc, SENSITIVE_FIELDS)
    except (ValueError, KeyError):
        return {
            k: v for k, v in doc.items()
            if k != "encryption_metadata" and not k.endswith("_search")
        }


async def _validate_return_relationships_and_quantity(
    *,
    gate_pass_id: str,
    client_name: str,
    items: list[dict],
    delivery_id: Optional[str] = None,
    exclude_return_id: Optional[str] = None,
) -> None:
    """Check return links and quantities against active canonical movements."""
    gp_oid = parse_object_id(gate_pass_id, "Gate Pass ID")
    gp_raw = await gatepasses_collection.find_one({"_id": gp_oid})
    if not gp_raw:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    gp = _decrypted(gp_raw)
    if not be.same_client(client_name, gp.get("client_name")):
        raise HTTPException(status_code=400, detail="Return client does not match gate pass")
    if gp.get("status") in (be.DRAFT_STATUS, be.CANCELLED_STATUS):
        raise HTTPException(
            status_code=400,
            detail="Returns cannot be recorded against a draft or cancelled gate pass",
        )

    selected_delivery = None
    if delivery_id:
        dl_oid = parse_object_id(delivery_id, "Delivery ID")
        dl_raw = await deliveries_collection.find_one({"_id": dl_oid})
        if not dl_raw:
            raise HTTPException(status_code=404, detail="Delivery not found")
        selected_delivery = _decrypted(dl_raw)
        source_ids = be.source_gate_pass_ids(selected_delivery)
        if gate_pass_id not in source_ids:
            raise HTTPException(
                status_code=400,
                detail="Delivery does not belong to the specified gate pass",
            )
        if not be.same_client(client_name, selected_delivery.get("client_name")):
            raise HTTPException(status_code=400, detail="Return client does not match delivery")
        if selected_delivery.get("status") in (be.DRAFT_STATUS, be.CANCELLED_STATUS):
            raise HTTPException(
                status_code=400,
                detail="Returns can only be recorded against an active delivery",
            )

    deliveries, existing_returns = await ops.load_movements([gate_pass_id])
    active_deliveries = [
        doc for doc in deliveries
        if doc.get("status") not in (be.DRAFT_STATUS, be.CANCELLED_STATUS)
    ]
    delivered_by_gp = be.compute_delivered_by_gate_pass(active_deliveries).get(
        gate_pass_id, {}
    )
    delivered_from_selected = (
        be.compute_delivered_by_gate_pass([selected_delivery]).get(gate_pass_id, {})
        if selected_delivery else {}
    )

    previous_by_item: dict[str, float] = {}
    previous_selected_by_item: dict[str, float] = {}
    for ret in existing_returns:
        if exclude_return_id and str(ret.get("return_id")) == exclude_return_id:
            continue
        for item in ret.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            key = be.item_key(item.get("item_name", ""), item.get("specification"))
            qty = float(item.get("returned_qty", 0) or 0)
            previous_by_item[key] = previous_by_item.get(key, 0) + qty
            if selected_delivery and ret.get("delivery_id") in (None, delivery_id):
                previous_selected_by_item[key] = (
                    previous_selected_by_item.get(key, 0) + qty
                )

    requested_by_item: dict[str, float] = {}
    for item in items:
        key = be.item_key(item.get("item_name", ""), item.get("specification"))
        requested_by_item[key] = requested_by_item.get(key, 0) + float(
            item.get("returned_qty", 0) or 0
        )

    for key, requested in requested_by_item.items():
        delivered = float(delivered_by_gp.get(key, 0) or 0)
        previously_returned = previous_by_item.get(key, 0)
        available = max(0, delivered - previously_returned)
        if requested > available + 1e-9:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Return quantity for '{be.flatten_name(key)}' exceeds the "
                    f"delivered quantity available to return (requested {requested:g}, "
                    f"available {available:g})"
                ),
            )

        if selected_delivery:
            from_delivery = float(delivered_from_selected.get(key, 0) or 0)
            previously_returned_from_delivery = previous_selected_by_item.get(key, 0)
            delivery_available = max(0, from_delivery - previously_returned_from_delivery)
            if requested > delivery_available + 1e-9:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Return quantity for '{be.flatten_name(key)}' exceeds the "
                        f"quantity delivered in this delivery (available "
                        f"{delivery_available:g})"
                    ),
                )


async def _refresh_return_gate_pass_status(gate_pass_id: str) -> None:
    await ops.refresh_gate_pass_statuses([gate_pass_id])


@router.post("/returns")
async def create_return(
    payload: ReturnCreate,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Record a garment return from a client."""
    now = datetime.now(timezone.utc)
    return_id = _generate_return_id()

    items_data = [item.model_dump() for item in payload.items]
    await _validate_return_relationships_and_quantity(
        gate_pass_id=payload.gate_pass_id,
        client_name=payload.client_name,
        items=items_data,
        delivery_id=payload.delivery_id,
    )

    doc = {
        "return_id": return_id,
        "gate_pass_id": payload.gate_pass_id,
        "delivery_id": payload.delivery_id,
        "client_name": payload.client_name.strip(),
        "items": items_data,
        "bill_adjustment": payload.bill_adjustment.model_dump() if payload.bill_adjustment else None,
        "status": "PENDING",
        "recorded_by": current_user.get("user_name", ""),
        "notes": payload.notes,
        "created_at": now,
        "updated_at": now,
    }

    encrypted = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await returns_collection.insert_one(encrypted)
    # ensure searchable token present for filters
    await returns_collection.update_one({"_id": result.inserted_id}, {"$set": {"client_name_search": get_search_token(doc.get("client_name"))}})
    doc["_id"] = str(result.inserted_id)

    await log_audit(
        current_user.get("user_name", ""),
        "create",
        "return",
        str(result.inserted_id),
        details={"return_id": return_id, "client": payload.client_name, "items": len(payload.items)},
    )

    await record_event(
        entity_type="return",
        entity_id=str(result.inserted_id),
        event_type=EVENT_RETURN_CREATED,
        gate_pass_id=payload.gate_pass_id,
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=payload.notes,
        item_deltas=[
            build_item_delta(item.get("item_name"), item.get("specification"), 0, item.get("returned_qty", 0))
            for item in items_data
        ],
        meta={"return_id": return_id, "delivery_id": payload.delivery_id, "status": "PENDING"},
    )

    await _refresh_return_gate_pass_status(payload.gate_pass_id)
    return _dec(doc)


@router.get("/returns")
async def list_returns(
    client_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    gate_pass_id: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """List returns with optional filters."""
    query: dict = {}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)
    if status:
        query["status"] = status
    if gate_pass_id:
        query["gate_pass_id"] = gate_pass_id

    total = await returns_collection.count_documents(query)
    cursor = returns_collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        items.append(_dec(doc))

    return {"items": items, "total": total}


@router.get("/returns/pending-resent")
async def pending_resent_items(
    client_name: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """Get all returned items pending resend to clients."""
    query: dict = {}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)

    cursor = returns_collection.find(query).sort("created_at", -1)
    results = []
    async for doc in cursor:
        try:
            ser = _dec(doc)
        except Exception:
            continue
        pending_items = [
            item for item in ser.get("items", [])
            if item.get("action") in ("RECEIVE_BACK", "RE_WASH")
            and item.get("resend_status") != "SENT"
        ]
        if pending_items:
            results.append({
                "return_id": ser["return_id"],
                "client_name": ser["client_name"],
                "items": pending_items,
                "created_at": ser["created_at"],
            })

    return results


@router.get("/returns/stats/summary")
async def returns_summary(
    current_user: dict = Depends(require_capability("dashboard:read")),
):
    """Return stats summary for dashboard."""
    total = await returns_collection.count_documents({})
    pending = await returns_collection.count_documents({"status": "PENDING"})
    received = await returns_collection.count_documents({"status": "RECEIVED"})
    processed = await returns_collection.count_documents({"status": "PROCESSED"})
    return {
        "total": total,
        "pending": pending,
        "received": received,
        "processed": processed,
    }


@router.get("/returns/{return_id}")
async def get_return(
    return_id: str,
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """Get a single return by return_id."""
    raw_doc = await returns_collection.find_one({"return_id": return_id})
    if not raw_doc:
        raise HTTPException(status_code=404, detail="Return not found")
    return _dec(raw_doc)


@router.patch("/returns/{return_id}")
async def update_return(
    return_id: str,
    payload: ReturnUpdate,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Update a return record (status, items, adjustment)."""
    raw_doc = await returns_collection.find_one({"return_id": return_id})
    if not raw_doc:
        raise HTTPException(status_code=404, detail="Return not found")

    doc = _dec(raw_doc)

    if doc.get("status") == "PROCESSED" and payload.status and payload.status != "PROCESSED":
        raise HTTPException(status_code=400, detail="Cannot modify a processed return")

    update_fields: dict = {"updated_at": datetime.now(timezone.utc)}

    if payload.status:
        if payload.status not in RETURN_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
        update_fields["status"] = payload.status

    if payload.items is not None:
        update_fields["items"] = [item.model_dump() for item in payload.items]

    if payload.bill_adjustment is not None:
        update_fields["bill_adjustment"] = payload.bill_adjustment.model_dump()

    if payload.notes is not None:
        update_fields["notes"] = payload.notes

    # Merge with existing decrypted doc, then re-encrypt entire document
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged.update(update_fields)
    await _validate_return_relationships_and_quantity(
        gate_pass_id=merged.get("gate_pass_id", ""),
        client_name=merged.get("client_name", ""),
        items=merged.get("items", []),
        delivery_id=merged.get("delivery_id"),
        exclude_return_id=return_id,
    )
    encrypted = encrypt_dict(merged, SENSITIVE_FIELDS)
    await returns_collection.update_one({"return_id": return_id}, {"$set": encrypted})

    await log_audit(
        current_user.get("user_name", ""),
        "update",
        "return",
        str(raw_doc["_id"]),
        details={"return_id": return_id, "changes": list(update_fields.keys())},
    )

    await record_event(
        entity_type="return",
        entity_id=str(raw_doc["_id"]),
        event_type=EVENT_RETURN_UPDATED,
        gate_pass_id=doc.get("gate_pass_id"),
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=payload.notes,
        meta={"return_id": return_id, "changes": list(update_fields.keys())},
    )

    updated = await returns_collection.find_one({"return_id": return_id})
    await _refresh_return_gate_pass_status(doc.get("gate_pass_id", ""))
    return _dec(updated)


@router.post("/returns/{return_id}/resent")
async def mark_item_resent(
    return_id: str,
    item_name: str,
    specification: str = "",
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Mark a returned item as re-sent to client."""
    raw_doc = await returns_collection.find_one({"return_id": return_id})
    if not raw_doc:
        raise HTTPException(status_code=404, detail="Return not found")

    doc = _dec(raw_doc)
    await _validate_return_relationships_and_quantity(
        gate_pass_id=doc.get("gate_pass_id", ""),
        client_name=doc.get("client_name", ""),
        items=doc.get("items", []),
        delivery_id=doc.get("delivery_id"),
        exclude_return_id=return_id,
    )

    now = datetime.now(timezone.utc)
    updated_items = []
    found = False
    for item in doc.get("items", []):
        if (
            item.get("item_name") == item_name
            and item.get("specification", "") == specification
            and item.get("action") in ("RECEIVE_BACK", "RE_WASH")
        ):
            item["resend_status"] = "SENT"
            item["resent_at"] = now.isoformat()
            found = True
        updated_items.append(item)

    if not found:
        raise HTTPException(status_code=400, detail="Item not found or not eligible for resend")

    # Merge updated items with full decrypted doc, then re-encrypt everything
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["items"] = updated_items
    merged["updated_at"] = now
    encrypted = encrypt_dict(merged, SENSITIVE_FIELDS)
    await returns_collection.update_one(
        {"return_id": return_id},
        {"$set": encrypted},
    )

    await log_audit(
        current_user.get("user_name", ""),
        "resent",
        "return",
        str(raw_doc["_id"]),
        details={"return_id": return_id, "item": item_name, "spec": specification},
    )

    await record_event(
        entity_type="return",
        entity_id=str(raw_doc["_id"]),
        event_type=EVENT_RETURN_RESENT,
        gate_pass_id=doc.get("gate_pass_id"),
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        item_deltas=[
            build_item_delta(item_name, specification, 0, 0)
        ],
        meta={"return_id": return_id, "resent_at": now.isoformat()},
    )

    updated = await returns_collection.find_one({"return_id": return_id})
    await _refresh_return_gate_pass_status(doc.get("gate_pass_id", ""))
    return _dec(updated)

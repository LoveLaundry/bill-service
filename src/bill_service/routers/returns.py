"""Returns router — record garment returns from clients."""
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import returns_collection, gatepasses_collection, deliveries_collection
from ..gatepass_balance import resync_gate_pass_status
from ..models import ReturnCreate, ReturnUpdate, RETURN_STATUSES
from ..router_utils import parse_object_id, log_audit
from ..services import balance_engine as be
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_RETURN_CREATED,
    EVENT_RETURN_RESENT,
    EVENT_RETURN_UPDATED,
)
from ..crypto_helper import get_search_token, encrypt_dict, decrypt_dict

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


async def _resync_pass(gate_pass_id: Optional[str]) -> Optional[str]:
    """Re-derive the linked gate pass, tolerating a return with no usable link.

    A return always moves the balance of its gate pass, so the pass status has to
    follow it — otherwise a balanced pass keeps a stale status and stays hidden
    from the delivery form. A legacy return whose gate_pass_id is missing or
    unreadable must not fail the write that the operator actually asked for, so
    the resync is best-effort and reported as ``None``.
    """
    if not gate_pass_id:
        return None
    try:
        return await resync_gate_pass_status(gate_pass_id)
    except HTTPException:
        return None


def _pass_item_keys(gp_dec: dict) -> set:
    return {
        be.item_key(it.get("item_name", ""), it.get("specification"))
        for it in (gp_dec.get("items") or [])
    }


def _reject_items_not_on_pass(gp_dec: dict, items, gate_pass_id: str) -> None:
    """Refuse a return for an item the gate pass never carried.

    Returns feed the balance directly (``outstanding = received - delivered +
    returned + adjustment``), so an unvalidated return manufactures outstanding
    pieces. The operator is then asked to deliver laundry that was never
    received, and the balance can only be cleared by a correction that papered
    over the mistake.
    """
    known = _pass_item_keys(gp_dec)
    unknown = []
    for item in items or []:
        key = be.item_key(item.item_name, item.specification)
        if key not in known and key not in unknown:
            unknown.append(key)
    if not unknown:
        return
    listed = ", ".join(
        f"{k} (spec: {k.split('||', 1)[1]})" if "||" in k and k.split("||", 1)[1] else k
        for k in unknown
    )
    raise HTTPException(
        status_code=400,
        detail=(
            f"{listed} was not received on this gate pass, so it cannot be "
            "returned against it. Check the item name, or record the missing "
            "pieces on the gate pass first."
        ),
    )


@router.post("/returns")
async def create_return(
    payload: ReturnCreate,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Record a garment return from a client."""
    # Validate gate pass exists
    gp_oid = parse_object_id(payload.gate_pass_id)
    gp_doc = await gatepasses_collection.find_one({"_id": gp_oid})
    if not gp_doc:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    gp_dec = decrypt_dict(gp_doc, SENSITIVE_FIELDS)

    # Validate delivery if provided
    if payload.delivery_id:
        dl_oid = parse_object_id(payload.delivery_id)
        dl_doc = await deliveries_collection.find_one({"_id": dl_oid})
        if not dl_doc:
            raise HTTPException(status_code=404, detail="Delivery not found")
        # A return raises the balance of ITS OWN pass. Pointing it at a delivery
        # from a different pass credited pieces to a gate pass that never sent
        # them, which is how a balance ends up owed for laundry that was never
        # involved.
        if dl_doc.get("gate_pass_id") != payload.gate_pass_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "That delivery belongs to a different gate pass, so returning "
                    "it against this pass would credit pieces to the wrong "
                    "balance."
                ),
            )

    # Every returned item has to be one the pass actually carries. A return for
    # an item that was never received creates outstanding pieces out of nothing,
    # and the client is then chased for laundry that does not exist.
    _reject_items_not_on_pass(gp_dec, payload.items, payload.gate_pass_id)

    now = datetime.now(timezone.utc)
    return_id = _generate_return_id()

    items_data = [item.model_dump() for item in payload.items]

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
    doc["_id"] = str(result.inserted_id)

    # A return puts pieces back on the pass, so the balance moves and the stored
    # status has to move with it. Without this a fully-delivered pass stays
    # labelled DELIVERED and disappears from the delivery form even though the
    # client is now owed those pieces again.
    status_after = await _resync_pass(payload.gate_pass_id)

    await log_audit(
        current_user.get("user_name", ""),
        "create",
        "return",
        str(result.inserted_id),
        details={
            "return_id": return_id,
            "client": payload.client_name,
            "items": len(payload.items),
            "gate_pass_status": status_after,
        },
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
        meta={"return_id": return_id, "delivery_id": payload.delivery_id, "status": "PENDING", "gate_pass_status": status_after},
    )

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
        gp_id = doc.get("gate_pass_id")
        if gp_id:
            # Editing items can change what the return contributes to the
            # balance, so the same "must exist on the pass" rule applies as on
            # create — otherwise an edit is a way around it.
            try:
                gp_doc = await gatepasses_collection.find_one(
                    {"_id": parse_object_id(gp_id)}
                )
            except HTTPException:
                gp_doc = None
            if gp_doc:
                _reject_items_not_on_pass(
                    decrypt_dict(gp_doc, SENSITIVE_FIELDS), payload.items, gp_id
                )
        update_fields["items"] = [item.model_dump() for item in payload.items]

    if payload.bill_adjustment is not None:
        update_fields["bill_adjustment"] = payload.bill_adjustment.model_dump()

    if payload.notes is not None:
        update_fields["notes"] = payload.notes

    # Merge with existing decrypted doc, then re-encrypt entire document
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged.update(update_fields)
    encrypted = encrypt_dict(merged, SENSITIVE_FIELDS)
    await returns_collection.update_one({"return_id": return_id}, {"$set": encrypted})

    # Editing a return can change what it contributes to the balance (items,
    # their action, or their resend flag), so the pass is re-derived again.
    status_after = await _resync_pass(doc.get("gate_pass_id"))

    await log_audit(
        current_user.get("user_name", ""),
        "update",
        "return",
        str(raw_doc["_id"]),
        details={
            "return_id": return_id,
            "changes": list(update_fields.keys()),
            "gate_pass_status": status_after,
        },
    )

    await record_event(
        entity_type="return",
        entity_id=str(raw_doc["_id"]),
        event_type=EVENT_RETURN_UPDATED,
        gate_pass_id=doc.get("gate_pass_id"),
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        reason=payload.notes,
        meta={
            "return_id": return_id,
            "changes": list(update_fields.keys()),
            "gate_pass_status": status_after,
        },
    )

    updated = await returns_collection.find_one({"return_id": return_id})
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

    now = datetime.now(timezone.utc)
    updated_items = []
    found = False
    # A specification is stored as None when the item has none, but every caller
    # sends "" for that case, so both sides are normalised before comparing.
    # Comparing raw values made this endpoint reject every un-specified item,
    # which is most of them, so a re-sent could never actually be recorded.
    wanted_spec = (specification or "").strip()
    for item in doc.get("items", []):
        if (
            item.get("item_name") == item_name
            and (item.get("specification") or "").strip() == wanted_spec
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

    # Re-sending takes the piece off the pending-return balance, which can close
    # the pass again. Re-derive so a pass cannot stay PARTIALLY_DELIVERED after
    # the last outstanding piece went back out.
    status_after = await _resync_pass(doc.get("gate_pass_id"))

    await log_audit(
        current_user.get("user_name", ""),
        "resent",
        "return",
        str(raw_doc["_id"]),
        details={
            "return_id": return_id,
            "item": item_name,
            "spec": specification,
            "gate_pass_status": status_after,
        },
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
        meta={
            "return_id": return_id,
            "resent_at": now.isoformat(),
            "gate_pass_status": status_after,
        },
    )

    updated = await returns_collection.find_one({"return_id": return_id})
    return _dec(updated)

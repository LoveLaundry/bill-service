from datetime import datetime, timezone
from typing import Dict, List, Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

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
from ..services.transaction_events import build_item_delta, record_event, EVENT_DELIVERY_CREATED
from ..services.verification_service import attach_verification_to
from ..models import DeliveryCreate, DeliveryModel

router = APIRouter(prefix="/deliveries", tags=["deliveries"])

SENSITIVE_FIELDS = ["client_name", "items", "notes"]
GATEPASS_SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def _serialize(doc: dict) -> dict:
    try:
        decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to decrypt document: {str(e)}"
        )

    decrypted["id"] = str(decrypted["_id"])
    del decrypted["_id"]
    return decrypted


def _parse_object_id(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ID format"
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

    gp_oid = _parse_object_id(payload.gate_pass_id)

    # 1. Fetch and decrypt Gate Pass
    gp_doc = await gatepasses_collection.find_one({"_id": gp_oid})
    if not gp_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Referenced Gate Pass not found"
        )

    try:
        gp_decrypted = decrypt_dict(gp_doc, GATEPASS_SENSITIVE_FIELDS)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to decrypt Gate Pass: {str(e)}"
        )

    # Map composite_key (item_name + specification) -> actual received quantity
    def _item_key(name: str, spec: str | None) -> str:
        return f"{name}||{spec or ''}"

    received_map: dict[str, int] = {}
    for gp_item in gp_decrypted.get("items", []):
        key = _item_key(gp_item["item_name"], gp_item.get("specification"))
        received_map[key] = received_map.get(key, 0) + gp_item["received_qty"]

    # 2. Query previous deliveries for this Gate Pass
    prev_deliveries_cursor = deliveries_collection.find(
        {"gate_pass_id": payload.gate_pass_id, "status": {"$ne": "CANCELLED"}}
    )
    delivered_map: dict[str, int] = {}
    async for prev_del_doc in prev_deliveries_cursor:
        try:
            prev_decrypted = decrypt_dict(prev_del_doc, SENSITIVE_FIELDS)
        except Exception:
            continue
        for prev_item in prev_decrypted.get("items", []):
            key = _item_key(prev_item["item_name"], prev_item.get("specification"))
            delivered_map[key] = delivered_map.get(key, 0) + prev_item["quantity"]

    # 3. Validate new delivery quantities against available balance
    new_delivery_items = []
    for item in payload.items:
        key = _item_key(item.item_name, item.specification)
        req_qty = item.quantity

        if key not in received_map:
            detail_msg = f"Item '{item.item_name}'"
            if item.specification:
                detail_msg += f" ({item.specification})"
            detail_msg += " was not received in the original Gate Pass."
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=detail_msg,
            )

        actual_rec = received_map[key]
        already_del = delivered_map.get(key, 0)
        available = actual_rec - already_del

        if req_qty > available:
            detail_msg = f"Cannot deliver {req_qty} of '{item.item_name}'"
            if item.specification:
                detail_msg += f" ({item.specification})"
            detail_msg += f". Only {available} available (Received: {actual_rec}, Already delivered: {already_del})."
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=detail_msg,
            )

        new_delivery_items.append(
            {
                "item_name": item.item_name,
                "specification": item.specification,
                "quantity": req_qty,
            }
        )

    # 4. Insert delivery record
    now = datetime.now(timezone.utc)
    delivery_doc = {
        "gate_pass_id": payload.gate_pass_id,
        "client_name": payload.client_name,
        "delivery_date": payload.delivery_date.replace(tzinfo=timezone.utc),
        "delivered_by": payload.delivered_by,
        "received_by": payload.received_by,
        "items": new_delivery_items,
        "status": "DELIVERED",
        "notes": payload.notes,
        "created_at": now,
    }

    encrypted_delivery = encrypt_dict(delivery_doc, SENSITIVE_FIELDS)
    result = await deliveries_collection.insert_one(encrypted_delivery)
    created = await deliveries_collection.find_one({"_id": result.inserted_id})
    serialized = _serialize(created)

    new_version = await bump_version("delivery", result.inserted_id)
    await enqueue_sync("delivery", result.inserted_id, new_version)
    serialized = await attach_verification_to("delivery", result.inserted_id, serialized)

    # 5. Check if Gate Pass is now fully delivered
    # Recalculate combined delivered maps after this successful write
    updated_delivered_map = delivered_map.copy()
    for item in new_delivery_items:
        key = _item_key(item["item_name"], item.get("specification"))
        updated_delivered_map[key] = updated_delivered_map.get(key, 0) + item["quantity"]

    fully_delivered = True
    for key, rec_qty in received_map.items():
        del_qty = updated_delivered_map.get(key, 0)
        if del_qty < rec_qty:
            fully_delivered = False
            break

    new_gp_status = "DELIVERED" if fully_delivered else "PARTIALLY_DELIVERED"
    await gatepasses_collection.update_one(
        {"_id": gp_oid},
        {
            "$set": {
                "status": new_gp_status,
                "updated_at": now,
            }
        },
    )

    gp_new_version = await bump_version("gatepass", gp_oid)
    await enqueue_sync("gatepass", gp_oid, gp_new_version)

    await record_event(
        entity_type="delivery",
        entity_id=serialized["id"],
        event_type=EVENT_DELIVERY_CREATED,
        gate_pass_id=payload.gate_pass_id,
        user_id=current_user.get("auth_id", "system"),
        user_name=current_user.get("user_name"),
        item_deltas=[
            build_item_delta(item["item_name"], item.get("specification"), 0, item["quantity"])
            for item in new_delivery_items
        ],
        reason=payload.notes,
        prev_status=gp_decrypted.get("status"),
        new_status=new_gp_status,
        meta={"delivery_date": payload.delivery_date.isoformat(), "fully_delivered": fully_delivered},
    )

    await log_audit(
        current_user.get("auth_id", "system"),
        "DELIVERY_CREATE",
        "delivery",
        serialized["id"],
    )
    await idempotency.record_created(request, auth_id, "delivery", serialized["id"])
    return serialized


@router.get("/pending-gatepasses")
async def pending_gatepasses(
    client_name: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("delivery:read")),
):
    """Return gate passes with actual pending items (received - delivered + returned).

    Used by the multi-select delivery form to show which gate passes have
    items that still need to be sent.
    """
    query: dict = {"status": {"$nin": ["DELIVERED", "CANCELLED"]}}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)

    # Pre-fetch all deliveries and returns to compute delivered/returned maps
    del_cursor = deliveries_collection.find({"status": {"$ne": "CANCELLED"}})
    all_deliveries: List[dict] = []
    async for doc in del_cursor:
        try:
            all_deliveries.append(decrypt_dict(doc, SENSITIVE_FIELDS))
        except Exception:
            continue

    # Build delivered map: gate_pass_id → {item_key → qty} via the canonical engine.
    from ..services import balance_engine as be

    delivered_by_gp: Dict[str, Dict[str, int]] = {}
    for dl in all_deliveries:
        dm = be.compute_delivered_by_item([dl])
        cur = delivered_by_gp.setdefault(dl.get("gate_pass_id", ""), {})
        for k, v in dm.items():
            cur[k] = cur.get(k, 0) + v

    # Build returned map via the canonical engine (only RECEIVE_BACK/RE_WASH not SENT).
    ret_cursor = returns_collection.find()
    returned_by_gp: Dict[str, Dict[str, int]] = {}
    async for doc in ret_cursor:
        try:
            ret = decrypt_dict(doc, GATEPASS_SENSITIVE_FIELDS)
        except Exception:
            continue
        gp_id = ret.get("gate_pass_id") or ""
        if not gp_id:
            continue
        rm = be.compute_returned_by_item([ret])
        cur = returned_by_gp.setdefault(gp_id, {})
        for k, v in rm.items():
            cur[k] = cur.get(k, 0) + v

    # Process gate passes
    gp_cursor = gatepasses_collection.find(query).sort("receiving_date", -1)
    results = []
    async for doc in gp_cursor:
        try:
            gp = decrypt_dict(doc, GATEPASS_SENSITIVE_FIELDS)
        except Exception:
            continue

        gp_id = gp.get("id") or str(doc["_id"])
        client = (gp.get("client_name") or "").strip()

        balance = be.compute_gate_pass_balance(
            gp.get("items", []),
            delivered_by_gp.get(gp_id, {}),
            returned_by_gp.get(gp_id, {}),
            marked_delivered=bool(gp.get("marked_delivered")),
        )

        items_with_pending = be.compute_outstanding_per_item(
            gp.get("items", []), balance
        )
        # Keep the endpoint's canonical output field names.
        items_with_pending = [
            {
                "item_name": r["item_name"],
                "specification": r["specification"],
                "category": r["category"],
                "received_qty": r["received_qty"],
                "delivered_qty": r["delivered_qty"],
                "returned_qty": r["returned_qty"],
                "pending_qty": r["pending_qty"],
            }
            for r in items_with_pending
        ]

        if items_with_pending:
            total_pending = sum(i["pending_qty"] for i in items_with_pending)
            results.append({
                "gate_pass_id": gp_id,
                "gate_pass_number": gp.get("gate_pass_number", ""),
                "client_name": client,
                "receiving_date": str(gp.get("receiving_date", ""))[:10],
                "status": gp.get("status", ""),
                "total_pending": total_pending,
                "items": items_with_pending,
            })

    return results


@router.get("", response_model=List[DeliveryModel])
async def list_deliveries(
    client_name: Optional[str] = Query(None),
    gate_pass_id: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    current_user: dict = Depends(require_capability("delivery:read")),
):
    query = {}

    if client_name:
        query["client_name_search"] = get_search_token(client_name)

    if gate_pass_id:
        query["gate_pass_id"] = gate_pass_id

    if date_from or date_to:
        date_query = {}
        if date_from:
            date_query["$gte"] = date_from.replace(tzinfo=timezone.utc)
        if date_to:
            date_query["$lte"] = date_to.replace(tzinfo=timezone.utc)
        query["delivery_date"] = date_query

    cursor = deliveries_collection.find(query).sort("delivery_date", -1)
    results = []
    async for doc in cursor:
        try:
            serialized = _serialize(doc)
            results.append(await attach_verification_to("delivery", doc["_id"], serialized))
        except HTTPException:
            pass
    return results


@router.get("/{delivery_id}", response_model=DeliveryModel)
async def get_delivery(
    delivery_id: str,
    current_user: dict = Depends(require_capability("delivery:read")),
):
    oid = _parse_object_id(delivery_id)
    doc = await deliveries_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery record not found"
        )
    serialized = _serialize(doc)
    return await attach_verification_to("delivery", oid, serialized)

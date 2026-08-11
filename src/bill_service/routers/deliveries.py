from datetime import datetime, timezone
from typing import List, Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database import (
    audit_collection,
    deliveries_collection,
    gatepasses_collection,
)
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
):
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

    # Map item_name -> actual received quantity
    received_map = {}
    for gp_item in gp_decrypted.get("items", []):
        received_map[gp_item["item_name"]] = gp_item["received_qty"]

    # 2. Query previous deliveries for this Gate Pass
    prev_deliveries_cursor = deliveries_collection.find(
        {"gate_pass_id": payload.gate_pass_id, "status": {"$ne": "CANCELLED"}}
    )
    delivered_map = {}
    async for prev_del_doc in prev_deliveries_cursor:
        try:
            prev_decrypted = decrypt_dict(prev_del_doc, SENSITIVE_FIELDS)
        except Exception:
            continue
        for prev_item in prev_decrypted.get("items", []):
            name = prev_item["item_name"]
            delivered_map[name] = delivered_map.get(name, 0) + prev_item["quantity"]

    # 3. Validate new delivery quantities against available balance
    new_delivery_items = []
    for item in payload.items:
        item_name = item.item_name
        req_qty = item.quantity

        if item_name not in received_map:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Item '{item_name}' was not received in the original Gate Pass.",
            )

        actual_rec = received_map[item_name]
        already_del = delivered_map.get(item_name, 0)
        available = actual_rec - already_del

        if req_qty > available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot deliver {req_qty} of '{item_name}'. "
                    f"Only {available} available (Received: {actual_rec}, Already delivered: {already_del})."
                ),
            )

        new_delivery_items.append(
            {
                "item_name": item_name,
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

    # 5. Check if Gate Pass is now fully delivered
    # Recalculate combined delivered maps after this successful write
    updated_delivered_map = delivered_map.copy()
    for item in new_delivery_items:
        name = item["item_name"]
        updated_delivered_map[name] = updated_delivered_map.get(name, 0) + item["quantity"]

    fully_delivered = True
    for item_name, rec_qty in received_map.items():
        del_qty = updated_delivered_map.get(item_name, 0)
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

    await log_audit(
        current_user.get("auth_id", "system"),
        "DELIVERY_CREATE",
        "delivery",
        serialized["id"],
    )
    return serialized


@router.get("", response_model=List[DeliveryModel])
async def list_deliveries(
    client_name: Optional[str] = Query(None),
    gate_pass_id: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("delivery:read")),
):
    query = {}

    if client_name:
        query["client_name_search"] = get_search_token(client_name)

    if gate_pass_id:
        query["gate_pass_id"] = gate_pass_id

    cursor = deliveries_collection.find(query).sort("delivery_date", -1)
    results = []
    async for doc in cursor:
        try:
            results.append(_serialize(doc))
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
    return _serialize(doc)

from datetime import datetime, timezone
from typing import List, Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import audit_collection, gatepasses_collection
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services.verification_service import attach_verification_to
from ..models import (
    GatePassAdjustment,
    GatePassCreate,
    GatePassDateUpdate,
    GatePassModel,
    GatePassUpdate,
)

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
):
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

    await log_audit(
        current_user.get("auth_id", "system"),
        "RECEIVING_CREATE",
        "gatepass",
        serialized["id"],
    )
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


@router.patch("/{gate_pass_id}/status", response_model=GatePassModel)
async def update_gate_pass_status(
    gate_pass_id: str,
    status_update: str = Query(...),
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    valid_statuses = [
        "RECEIVED",
        "PROCESSING",
        "READY_FOR_DELIVERY",
        "PARTIALLY_DELIVERED",
        "DELIVERED",
        "CANCELLED",
    ]
    if status_update not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of {valid_statuses}",
        )

    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
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

    await log_audit(
        current_user.get("auth_id", "system"),
        "RECEIVING_STATUS_UPDATE",
        "gatepass",
        serialized["id"],
    )
    return serialized


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
    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )

    if doc.get("status") in ("DELIVERED", "CANCELLED"):
        raise HTTPException(
            status_code=409,
            detail=f"Gate Pass cannot be edited once it is {doc.get('status')}.",
        )

    # Decrypt original so encrypted fields can be updated wholesale.
    decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    update_data = payload.model_dump(exclude_unset=True)

    if "items" in update_data and update_data["items"]:
        processed_items = []
        for item in update_data["items"]:
            processed_items.append(
                {
                    "item_name": item.item_name,
                    "category": item.category,
                    "specification": item.specification,
                    "client_qty": item.client_qty,
                    "received_qty": item.received_qty,
                    "difference": item.received_qty - item.client_qty,
                    "mismatch_reason": item.mismatch_reason,
                    "mismatch_notes": item.mismatch_notes,
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

    # Re-encrypt and save
    encrypted_new = encrypt_dict(decrypted, SENSITIVE_FIELDS)
    await gatepasses_collection.replace_one({"_id": oid}, encrypted_new)

    updated_doc = await gatepasses_collection.find_one({"_id": oid})
    serialized = _serialize(updated_doc)

    new_version = await bump_version("gatepass", oid)
    await enqueue_sync("gatepass", oid, new_version)
    serialized = await attach_verification_to("gatepass", oid, serialized)

    await log_audit(
        current_user.get("auth_id", "system"),
        "RECEIVING_UPDATE",
        "gatepass",
        serialized["id"],
    )
    return serialized


@router.post("/{gate_pass_id}/adjust", response_model=GatePassModel)
async def adjust_gate_pass(
    gate_pass_id: str,
    payload: GatePassAdjustment,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    oid = _parse_object_id(gate_pass_id)
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gate Pass not found"
        )

    # Decrypt original
    decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)

    # Locate the item to adjust
    items = decrypted.get("items", [])
    found = False
    original_val = None
    for item in items:
        if item["item_name"] == payload.item_name:
            original_val = item["received_qty"]
            item["received_qty"] = payload.corrected_qty
            item["difference"] = payload.corrected_qty - item["client_qty"]
            found = True
            break

    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{payload.item_name}' not found in Gate Pass items.",
        )

    # Add adjustment record
    adjustments = decrypted.get("adjustments", [])
    adjustments.append(
        {
            "user_id": current_user.get("auth_id", "system"),
            "timestamp": datetime.now(timezone.utc),
            "item_name": payload.item_name,
            "original_value": original_val,
            "corrected_value": payload.corrected_qty,
            "reason": payload.reason,
        }
    )
    decrypted["adjustments"] = adjustments
    decrypted["updated_at"] = datetime.now(timezone.utc)

    # Re-encrypt and save
    encrypted_new = encrypt_dict(decrypted, SENSITIVE_FIELDS)
    await gatepasses_collection.replace_one({"_id": oid}, encrypted_new)

    updated_doc = await gatepasses_collection.find_one({"_id": oid})
    serialized = _serialize(updated_doc)

    new_version = await bump_version("gatepass", oid)
    await enqueue_sync("gatepass", oid, new_version)
    serialized = await attach_verification_to("gatepass", oid, serialized)

    await log_audit(
        current_user.get("auth_id", "system"),
        "RECEIVING_ADJUST",
        "gatepass",
        serialized["id"],
    )
    return serialized

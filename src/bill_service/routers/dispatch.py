from datetime import datetime, timezone
from typing import List, Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import (
    audit_collection,
    dispatch_jobs_collection,
)
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services.verification_service import attach_verification_to
from ..models import DispatchCreate, DispatchUpdate, DispatchModel

router = APIRouter(prefix="/dispatch", tags=["dispatch"])

SENSITIVE_FIELDS = ["client_name", "address", "contact_name", "contact_phone", "notes"]

DISPATCH_STATUSES = ["SCHEDULED", "ASSIGNED", "EN_ROUTE", "COMPLETED", "CANCELLED"]


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


@router.post("", response_model=DispatchModel, status_code=status.HTTP_201_CREATED)
async def create_dispatch_job(
    payload: DispatchCreate,
    current_user: dict = Depends(require_capability("dispatch:write")),
):
    if payload.job_type not in ("pickup", "delivery"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="job_type must be 'pickup' or 'delivery'",
        )

    now = datetime.now(timezone.utc)
    doc = {
        "job_type": payload.job_type,
        "order_id": payload.order_id,
        "client_name": payload.client_name,
        "address": payload.address,
        "contact_name": payload.contact_name,
        "contact_phone": payload.contact_phone,
        "scheduled_at": payload.scheduled_at.replace(tzinfo=timezone.utc)
        if payload.scheduled_at
        else None,
        "status": "SCHEDULED",
        "assigned_to": payload.assigned_to,
        "notes": payload.notes,
        "client_name_search": get_search_token(payload.client_name),
        "created_at": now,
        "updated_at": now,
    }

    encrypted = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await dispatch_jobs_collection.insert_one(encrypted)
    created = await dispatch_jobs_collection.find_one({"_id": result.inserted_id})
    serialized = _serialize(created)

    new_version = await bump_version("dispatch", result.inserted_id)
    await enqueue_sync("dispatch", result.inserted_id, new_version)
    serialized = await attach_verification_to("dispatch", result.inserted_id, serialized)

    await log_audit(
        current_user.get("auth_id", "system"),
        "DISPATCH_CREATE",
        "dispatch",
        serialized["id"],
    )
    return serialized


@router.get("", response_model=List[DispatchModel])
async def list_dispatch_jobs(
    client_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    assigned_to: Optional[str] = Query(None),
    job_type: Optional[str] = Query(None),
    scheduled_from: Optional[str] = Query(None),
    scheduled_to: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("dispatch:read")),
):
    query: dict = {}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)
    if status:
        query["status"] = status
    if assigned_to:
        query["assigned_to"] = assigned_to
    if job_type:
        query["job_type"] = job_type
    if scheduled_from or scheduled_to:
        query["scheduled_at"] = {}
        if scheduled_from:
            query["scheduled_at"]["$gte"] = scheduled_from
        if scheduled_to:
            query["scheduled_at"]["$lte"] = scheduled_to

    cursor = dispatch_jobs_collection.find(query).sort("scheduled_at", 1)
    results = []
    async for doc in cursor:
        try:
            serialized = _serialize(doc)
            results.append(
                await attach_verification_to("dispatch", doc["_id"], serialized)
            )
        except HTTPException:
            pass
    return results


@router.get("/{job_id}", response_model=DispatchModel)
async def get_dispatch_job(
    job_id: str,
    current_user: dict = Depends(require_capability("dispatch:read")),
):
    oid = _parse_object_id(job_id)
    doc = await dispatch_jobs_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dispatch job not found"
        )
    serialized = _serialize(doc)
    return await attach_verification_to("dispatch", oid, serialized)


@router.patch("/{job_id}", response_model=DispatchModel)
async def update_dispatch_job(
    job_id: str,
    payload: DispatchUpdate,
    current_user: dict = Depends(require_capability("dispatch:write")),
):
    oid = _parse_object_id(job_id)
    doc = await dispatch_jobs_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dispatch job not found"
        )

    update_data: dict = {}
    if payload.status is not None:
        if payload.status not in DISPATCH_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"status must be one of {DISPATCH_STATUSES}",
            )
        update_data["status"] = payload.status
    if payload.assigned_to is not None:
        update_data["assigned_to"] = payload.assigned_to
    if payload.scheduled_at is not None:
        update_data["scheduled_at"] = payload.scheduled_at.replace(tzinfo=timezone.utc)
    if payload.notes is not None:
        update_data["notes"] = payload.notes

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update"
        )

    update_data["updated_at"] = datetime.now(timezone.utc)
    await dispatch_jobs_collection.update_one({"_id": oid}, {"$set": update_data})

    new_version = await bump_version("dispatch", oid)
    await enqueue_sync("dispatch", oid, new_version)

    updated = await dispatch_jobs_collection.find_one({"_id": oid})
    serialized = _serialize(updated)
    serialized = await attach_verification_to("dispatch", oid, serialized)

    await log_audit(
        current_user.get("auth_id", "system"),
        "DISPATCH_UPDATE",
        "dispatch",
        serialized["id"],
    )
    return serialized


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dispatch_job(
    job_id: str,
    current_user: dict = Depends(require_capability("dispatch:write")),
):
    oid = _parse_object_id(job_id)
    result = await dispatch_jobs_collection.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dispatch job not found"
        )
    new_version = await bump_version("dispatch", oid)
    await enqueue_sync("dispatch", oid, new_version)

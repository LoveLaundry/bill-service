"""Shared router utilities — deduplicates _serialize, _parse_object_id, log_audit across routers."""

import logging
from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException

from .crypto_helper import decrypt_dict


def parse_object_id(value: str, label: str = "ID") -> ObjectId:
    """Parse a string to ObjectId, raising 400 on failure."""
    try:
        return ObjectId(value)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value}")


def serialize(doc: dict, sensitive_fields: list[str]) -> dict:
    """Decrypt a MongoDB document and convert _id to string id."""
    try:
        decrypted = decrypt_dict(doc, sensitive_fields)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to decrypt document: {str(e)}"
        )
    decrypted["id"] = str(decrypted["_id"])
    del decrypted["_id"]
    return decrypted


async def log_audit(
    auth_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: Optional[dict] = None,
    audit_collection=None,
    laundry_id: Optional[str] = None,
) -> bool:
    """Write an audit log entry (the single schema for this service).

    Document keys are canonical (`auth_id` / `entity_type`), plus the legacy
    aliases `user_id` / `entity` that the report UI and older readers still
    render. Writing both keeps one writer while every consumer keeps working.
    Returns False if the write failed — audit loss must be observable.
    """
    if audit_collection is None:
        from .database.main_db import audit_collection as ac
        audit_collection = ac

    _now = datetime.now(timezone.utc)
    log_entry = {
        "auth_id": auth_id,
        "user_id": auth_id,
        "action": action,
        "entity_type": entity_type,
        "entity": entity_type,
        "entity_id": entity_id,
        "details": details or {},
        "timestamp": _now,
        # Legacy alias for readers still keying on created_at.
        "created_at": _now,
    }
    if laundry_id:
        log_entry["laundry_id"] = laundry_id
    try:
        await audit_collection.insert_one(log_entry)
        return True
    except Exception:
        logging.getLogger(__name__).warning(
            "audit write failed action=%s entity_type=%s entity_id=%s",
            action, entity_type, entity_id,
        )
        return False

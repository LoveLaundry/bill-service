"""Shared router utilities — deduplicates _serialize, _parse_object_id, log_audit across routers."""

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
):
    """Write an audit log entry."""
    if audit_collection is None:
        from .database.main_db import audit_collection as ac
        audit_collection = ac

    log_entry = {
        "auth_id": auth_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "details": details or {},
        "timestamp": datetime.now(timezone.utc),
    }
    await audit_collection.insert_one(log_entry)

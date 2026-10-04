"""Shared gate-pass record creation.

``POST /gatepasses`` (normal receiving) and the monthly receiving-day confirm
both create gate passes. To keep DRAFT semantics consistent, the two paths
share this builder: it creates a NORMAL gate-pass document, journaled, version
bumped, enqueued for sync, and verified — exactly like the router did. The
only differences are ``status`` (DRAFT for monthly-generated passes) and the
``origin`` trace.
"""
from datetime import datetime, timezone
import re
from typing import Optional

from bson import ObjectId

from ..app_time import wall_clock
from ..crypto_helper import decrypt_dict, encrypt_dict
from ..database.main_db import audit_collection, gatepasses_collection
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_GATE_PASS_CREATED,
)
from ..services.verification_service import attach_verification_to
from ..models import GatePassCreate

SENSITIVE_FIELDS = ["client_name", "items", "notes"]

ALLOWED_STATUSES = ("RECEIVED", "DRAFT")


def _serialize(doc: dict) -> dict:
    decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    decrypted["id"] = str(decrypted["_id"])
    del decrypted["_id"]
    if not isinstance(decrypted.get("items"), list):
        decrypted["items"] = []
    return decrypted


async def next_receiving_number(receiving_date: datetime) -> str:
    """Generate a unique ``GP-YYYYMMDD-XXXX`` number (server-authoritative).

    The XXXX sequence is scoped per day so numbers stay short and readable;
    collisions are impossible because the insert re-checks uniqueness.
    """
    base = receiving_date.strftime("GP-%Y%m%d")
    prefix = f"{base}-"
    # Find the highest existing same-day number to continue the sequence.
    latest = await gatepasses_collection.find_one(
        {"gate_pass_number": {"$regex": f"^{re.escape(prefix)}"}},
        sort=[("gate_pass_number", -1)],
    )
    if latest:
        start = int(latest["gate_pass_number"].split("-")[-1]) + 1
    else:
        start = 1
    while True:
        candidate = f"{prefix}{start:04d}"
        existing = await gatepasses_collection.find_one({"gate_pass_number": candidate})
        if not existing:
            return candidate
        start += 1


async def create_gate_pass_record(
    payload: GatePassCreate,
    user: dict,
    *,
    status: str = "RECEIVED",
    origin: Optional[dict] = None,
) -> dict:
    """Create a gate pass document and run the full journal/sync/verify chain.

    ``payload.gate_pass_number`` must already be unique (caller should pass a
    server-generated number for DRAFT creation, or the client number for the
    normal flow where uniqueness is enforced upfront).
    """
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unsupported gate pass status: {status}")

    processed_items = []
    for item in payload.items:
        processed_items.append(
            {
                "item_name": item.item_name,
                "category": item.category,
                "specification": item.specification,
                "client_qty": item.client_qty,
                "received_qty": item.received_qty,
                "difference": item.received_qty - item.client_qty,
                "unit": item.unit,
                "piece_count": item.piece_count,
                "mismatch_reason": item.mismatch_reason,
                "mismatch_notes": item.mismatch_notes,
                "rewashed": bool(item.rewashed),
            }
        )

    now = datetime.now(timezone.utc)
    doc = {
        "gate_pass_number": payload.gate_pass_number,
        "client_name": payload.client_name,
        # Same wall-clock handling as the normal receiving path, so a
        # monthly-generated pass sorts by the same day boundary.
        "receiving_date": wall_clock(payload.receiving_date),
        "received_by": payload.received_by,
        "items": processed_items,
        "status": status,
        "notes": payload.notes,
        "quotation_id": payload.quotation_id,
        "origin": origin,
        "created_at": now,
        "updated_at": now,
        "adjustments": [],
    }

    encrypted_doc = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await gatepasses_collection.insert_one(encrypted_doc)
    created = await gatepasses_collection.find_one({"_id": result.inserted_id})

    serialized = _serialize(created)

    new_version = await bump_version("gatepass", result.inserted_id)
    await enqueue_sync("gatepass", result.inserted_id, new_version)
    serialized = await attach_verification_to("gatepass", result.inserted_id, serialized)

    await record_event(
        entity_type="gatepass",
        entity_id=serialized["id"],
        event_type=EVENT_GATE_PASS_CREATED,
        user_id=user.get("auth_id", "system"),
        user_name=user.get("user_name"),
        new_status=status,
        item_deltas=[
            build_item_delta(item["item_name"], item.get("specification"), 0, item["received_qty"])
            for item in processed_items
        ],
        meta={
            "gate_pass_number": payload.gate_pass_number,
            "origin": origin,
            "draft": status == "DRAFT",
        },
    )

    await audit_collection.insert_one(
        {
            "user_id": user.get("auth_id", "system"),
            "action": "RECEIVING_CREATE",
            "entity": "gatepass",
            "entity_id": serialized["id"],
            "timestamp": datetime.now(timezone.utc),
        }
    )
    return serialized
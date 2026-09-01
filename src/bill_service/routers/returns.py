"""Returns router — record garment returns from clients."""
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import returns_collection, gatepasses_collection, deliveries_collection
from ..models import ReturnCreate, ReturnUpdate, RETURN_STATUSES
from ..router_utils import parse_object_id, serialize, log_audit
from ..crypto_helper import get_search_token

router = APIRouter(tags=["Returns"])

SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def _generate_return_id() -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"RT-{code}"


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

    # Validate delivery if provided
    if payload.delivery_id:
        dl_oid = parse_object_id(payload.delivery_id)
        dl_doc = await deliveries_collection.find_one({"_id": dl_oid})
        if not dl_doc:
            raise HTTPException(status_code=404, detail="Delivery not found")

    now = datetime.now(timezone.utc)
    return_id = _generate_return_id()

    doc = {
        "return_id": return_id,
        "gate_pass_id": payload.gate_pass_id,
        "delivery_id": payload.delivery_id,
        "client_name": payload.client_name.strip(),
        "client_name_search": get_search_token(payload.client_name),
        "items": [item.model_dump() for item in payload.items],
        "bill_adjustment": payload.bill_adjustment.model_dump() if payload.bill_adjustment else None,
        "status": "PENDING",
        "recorded_by": current_user.get("user_name", ""),
        "notes": payload.notes,
        "created_at": now,
        "updated_at": now,
    }

    result = await returns_collection.insert_one(doc)
    doc["_id"] = str(result.inserted_id)

    await log_audit(
        current_user.get("user_name", ""),
        "create",
        "return",
        str(result.inserted_id),
        details={"return_id": return_id, "client": payload.client_name, "items": len(payload.items)},
    )

    return serialize(doc, SENSITIVE_FIELDS)


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
        items.append(serialize(doc, SENSITIVE_FIELDS))

    return {"items": items, "total": total}


@router.get("/returns/{return_id}")
async def get_return(
    return_id: str,
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """Get a single return by return_id."""
    doc = await returns_collection.find_one({"return_id": return_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")
    return serialize(doc, SENSITIVE_FIELDS)


@router.patch("/returns/{return_id}")
async def update_return(
    return_id: str,
    payload: ReturnUpdate,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Update a return record (status, items, adjustment)."""
    doc = await returns_collection.find_one({"return_id": return_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Return not found")

    if doc["status"] in ("PROCESSED") and payload.status != "PROCESSED":
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

    await returns_collection.update_one({"return_id": return_id}, {"$set": update_fields})

    await log_audit(
        current_user.get("user_name", ""),
        "update",
        "return",
        str(doc["_id"]),
        details={"return_id": return_id, "changes": list(update_fields.keys())},
    )

    updated = await returns_collection.find_one({"return_id": return_id})
    return serialize(updated, SENSITIVE_FIELDS)


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

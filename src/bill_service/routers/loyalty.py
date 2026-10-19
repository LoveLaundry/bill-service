from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import get_current_user, require_capability
from ..tenancy import assert_same_laundry, owned_filter, resolve_laundry_id, scoped_query
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import loyalty_collection
from ..models import LoyaltyAccount, LoyaltyAdjust, loyalty_tier
from ..router_utils import log_audit

router = APIRouter(prefix="/loyalty", tags=["loyalty"])

SENSITIVE_FIELDS = ["client_name"]


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


@router.get("", response_model=List[LoyaltyAccount])
async def list_loyalty(
    client_name: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("loyalty:read")),
):
    query: dict = {}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)
    cursor = loyalty_collection.find(scoped_query(query, resolve_laundry_id(current_user))).sort("points", -1)
    results = []
    async for doc in cursor:
        try:
            results.append(_serialize(doc))
        except HTTPException:
            pass
    return results


@router.get("/{client_name}", response_model=LoyaltyAccount)
async def get_loyalty(
    client_name: str,
    current_user: dict = Depends(require_capability("loyalty:read")),
):
    laundry_id = resolve_laundry_id(current_user)
    doc = await loyalty_collection.find_one(
        {"client_name_search": get_search_token(client_name)}
    )
    assert_same_laundry(doc, laundry_id, "Loyalty account")
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No loyalty account found"
        )
    return _serialize(doc)


@router.post("/adjust", response_model=LoyaltyAccount)
async def adjust_loyalty(
    payload: LoyaltyAdjust,
    current_user: dict = Depends(require_capability("loyalty:write")),
):
    token = get_search_token(payload.client_name)
    now = datetime.now(timezone.utc)
    laundry_id = resolve_laundry_id(current_user)
    existing = await loyalty_collection.find_one(
        scoped_query({"client_name_search": token}, laundry_id)
    )

    if existing:
        new_points = max(0, (existing.get("points") or 0) + payload.delta_points)
        visits = (existing.get("visits") or 0) + (1 if payload.delta_points > 0 else 0)
        await loyalty_collection.update_one(
            owned_filter(existing["_id"], laundry_id),
            {
                "$set": {
                    "points": new_points,
                    "tier": loyalty_tier(new_points),
                    "visits": visits,
                    "updated_at": now,
                }
            },
        )
        updated = await loyalty_collection.find_one({"_id": existing["_id"]})
        await log_audit(
            current_user.get("auth_id", "system"),
            "LOYALTY_ADJUST",
            "loyalty",
            str(existing["_id"]),
            {"client_name": payload.client_name, "delta_points": payload.delta_points, "new_points": new_points},
            laundry_id=laundry_id,
        )
        return _serialize(updated)

    points = max(0, payload.delta_points)
    doc = {
        "laundry_id": laundry_id,
        "client_name": payload.client_name,
        "client_name_search": token,
        "points": points,
        "tier": loyalty_tier(points),
        "visits": 1 if points > 0 else 0,
        "created_at": now,
        "updated_at": now,
    }
    encrypted = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await loyalty_collection.insert_one(encrypted)
    created = await loyalty_collection.find_one({"_id": result.inserted_id})
    await log_audit(
        current_user.get("auth_id", "system"),
        "LOYALTY_CREATE",
        "loyalty",
        str(result.inserted_id),
        {"client_name": payload.client_name, "delta_points": payload.delta_points, "new_points": points},
        laundry_id=laundry_id,
    )
    return _serialize(created)

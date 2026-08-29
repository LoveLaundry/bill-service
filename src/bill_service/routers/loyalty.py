from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import loyalty_collection
from ..models import LoyaltyAccount, LoyaltyAdjust, loyalty_tier

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
    cursor = loyalty_collection.find(query).sort("points", -1)
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
    doc = await loyalty_collection.find_one(
        {"client_name_search": get_search_token(client_name)}
    )
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
    existing = await loyalty_collection.find_one({"client_name_search": token})

    if existing:
        new_points = max(0, (existing.get("points") or 0) + payload.delta_points)
        visits = (existing.get("visits") or 0) + (1 if payload.delta_points > 0 else 0)
        await loyalty_collection.update_one(
            {"_id": existing["_id"]},
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
        return _serialize(updated)

    points = max(0, payload.delta_points)
    doc = {
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
    return _serialize(created)

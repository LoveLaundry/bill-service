from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Query, status

from ..database import bills_collection
from ..models import BillCreate, BillListResponse, BillModel

router = APIRouter(prefix="/bills", tags=["bills"])


def _serialize(doc: dict) -> dict:
    doc["_id"] = str(doc["_id"])
    return doc


def _parse_object_id(bill_id: str) -> ObjectId:
    try:
        return ObjectId(bill_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bill id")


@router.post("", response_model=BillModel, status_code=status.HTTP_201_CREATED)
async def create_bill(payload: BillCreate):
    items_out = []
    total_quantity = 0
    total_amount = 0.0

    for item in payload.items:
        line_total = round(item.unit_price * item.quantity, 2)
        items_out.append(
            {
                "item_name": item.item_name,
                "category": item.category,
                "unit_price": item.unit_price,
                "quantity": item.quantity,
                "line_total": line_total,
            }
        )
        total_quantity += item.quantity
        total_amount += line_total

    now = datetime.now(timezone.utc)
    doc = {
        "quotation_id": payload.quotation_id,
        "client_name": payload.client_name,
        "quotation_title": payload.quotation_title,
        "items": items_out,
        "total_quantity": total_quantity,
        "total_amount": round(total_amount, 2),
        "notes": payload.notes,
        "created_at": now,
        "updated_at": now,
    }

    result = await bills_collection.insert_one(doc)
    created = await bills_collection.find_one({"_id": result.inserted_id})
    return _serialize(created)


@router.get("", response_model=BillListResponse)
async def list_bills(
    search: Optional[str] = Query(None),
    client_name: Optional[str] = Query(None),
    quotation_id: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    query: dict = {}

    if client_name:
        query["client_name"] = {"$regex": client_name, "$options": "i"}

    if quotation_id:
        query["quotation_id"] = quotation_id

    if date_from or date_to:
        date_filter: dict = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            inclusive_date_to = date_to
            if (
                date_to.hour == 0
                and date_to.minute == 0
                and date_to.second == 0
                and date_to.microsecond == 0
            ):
                inclusive_date_to = date_to + timedelta(days=1)
            date_filter["$lt"] = inclusive_date_to
        query["created_at"] = date_filter

    if search:
        regex = {"$regex": search, "$options": "i"}
        query["$or"] = [
            {"client_name": regex},
            {"quotation_title": regex},
            {"items.item_name": regex},
        ]

    total = await bills_collection.count_documents(query)
    cursor = bills_collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
    docs = [_serialize(doc) async for doc in cursor]
    return {"items": docs, "total": total}


@router.get("/{bill_id}", response_model=BillModel)
async def get_bill(bill_id: str):
    oid = _parse_object_id(bill_id)
    doc = await bills_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found")
    return _serialize(doc)


@router.delete("/{bill_id}")
async def delete_bill(bill_id: str):
    oid = _parse_object_id(bill_id)
    result = await bills_collection.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found")
    return {"message": "Bill deleted"}
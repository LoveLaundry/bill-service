"""Shop Bills router — direct billing for shops (no gate pass required)."""
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import shop_bills_collection, audit_collection
from ..models import ShopBillCreate, ShopBillUpdate, ShopBillPayment, SHOP_BILL_STATUSES, SHOP_PAYMENT_STATUSES
from ..router_utils import parse_object_id, log_audit
from ..crypto_helper import get_search_token, encrypt_dict, decrypt_dict

router = APIRouter(prefix="/shop-bills", tags=["Shop Bills"])

SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def _dec(doc: dict) -> dict:
    """Decrypt and convert _id to id."""
    try:
        decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    except (ValueError, KeyError):
        decrypted = {k: v for k, v in doc.items() if k != "encryption_metadata" and not k.endswith("_search")}
    if "_id" in decrypted:
        decrypted["id"] = str(decrypted["_id"])
        del decrypted["_id"]
    return decrypted


def _enc(doc: dict) -> dict:
    """Encrypt sensitive fields for storage."""
    to_encrypt = {k: v for k, v in doc.items() if k != "id" and k != "_id"}
    return encrypt_dict(to_encrypt, SENSITIVE_FIELDS)


def _generate_bill_number() -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"SB-{code}"


def _calc_totals(items: list, discounts: float = 0, transport_fee: float = 0, taxes: float = 0) -> dict:
    """Calculate totals from items."""
    total_quantity = sum(item.get("quantity", 0) for item in items)
    total_amount = sum(item.get("unit_price", 0) * item.get("quantity", 0) for item in items)
    grand_total = total_amount - discounts + transport_fee + taxes
    return {
        "total_quantity": total_quantity,
        "total_amount": round(total_amount, 2),
        "grand_total": round(max(0, grand_total), 2),
    }


@router.post("", status_code=201)
async def create_shop_bill(
    payload: ShopBillCreate,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Create a new shop bill."""
    now = datetime.now(timezone.utc)
    bill_number = payload.bill_number.strip() if payload.bill_number else _generate_bill_number()

    # Check for duplicate bill_number
    existing = await shop_bills_collection.find_one({"bill_number": bill_number})
    if existing:
        raise HTTPException(status_code=409, detail=f"Bill number {bill_number} already exists")

    items_data = [item.model_dump() for item in payload.items]
    for item in items_data:
        item["line_total"] = round(item["unit_price"] * item["quantity"], 2)

    totals = _calc_totals(
        items_data,
        discounts=payload.discounts or 0,
        transport_fee=payload.transport_fee or 0,
        taxes=payload.taxes or 0,
    )

    doc = {
        "bill_number": bill_number,
        "client_name": payload.client_name.strip(),
        "quotation_id": payload.quotation_id,
        "items": items_data,
        "total_quantity": totals["total_quantity"],
        "total_amount": totals["total_amount"],
        "discounts": payload.discounts or 0.0,
        "transport_fee": payload.transport_fee or 0.0,
        "taxes": payload.taxes or 0.0,
        "grand_total": totals["grand_total"],
        "status": "PENDING",
        "payment_status": "DRAFT",
        "paid_amount": 0.0,
        "outstanding_amount": totals["grand_total"],
        "notes": payload.notes,
        "delivery_date": payload.delivery_date,
        "tags": [],
        "created_at": now,
        "updated_at": now,
    }

    encrypted = _enc(doc)
    result = await shop_bills_collection.insert_one(encrypted)
    doc["id"] = str(result.inserted_id)

    await log_audit(
        current_user.get("user_name", ""),
        "create",
        "shop_bill",
        str(result.inserted_id),
        details={"bill_number": bill_number, "client": payload.client_name, "items": len(payload.items)},
    )

    return _dec(doc)


@router.get("")
async def list_shop_bills(
    client_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_capability("bill:read")),
):
    """List shop bills with optional filters."""
    query: dict = {}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)
    if status:
        query["status"] = status
    if payment_status:
        query["payment_status"] = payment_status
    if search:
        query["bill_number"] = {"$regex": search, "$options": "i"}

    total = await shop_bills_collection.count_documents(query)
    cursor = shop_bills_collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        items.append(_dec(doc))

    return {"items": items, "total": total}


@router.get("/{bill_id}")
async def get_shop_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:read")),
):
    """Get a single shop bill by ID or bill_number."""
    # Try by ObjectId first
    raw_doc = await shop_bills_collection.find_one({"_id": parse_object_id(bill_id, "Bill ID")})
    if not raw_doc:
        # Try by bill_number
        raw_doc = await shop_bills_collection.find_one({"bill_number": bill_id})
    if not raw_doc:
        raise HTTPException(status_code=404, detail="Shop bill not found")
    return _dec(raw_doc)


@router.patch("/{bill_id}")
async def update_shop_bill(
    bill_id: str,
    payload: ShopBillUpdate,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Update a shop bill."""
    raw_doc = await shop_bills_collection.find_one({"_id": parse_object_id(bill_id, "Bill ID")})
    if not raw_doc:
        raw_doc = await shop_bills_collection.find_one({"bill_number": bill_id})
    if not raw_doc:
        raise HTTPException(status_code=404, detail="Shop bill not found")

    doc = _dec(raw_doc)
    now = datetime.now(timezone.utc)
    update_fields: dict = {"updated_at": now}

    if payload.status is not None:
        if payload.status not in SHOP_BILL_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
        update_fields["status"] = payload.status

    if payload.payment_status is not None:
        if payload.payment_status not in SHOP_PAYMENT_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid payment status: {payload.payment_status}")
        update_fields["payment_status"] = payload.payment_status

    if payload.notes is not None:
        update_fields["notes"] = payload.notes

    if payload.delivery_date is not None:
        update_fields["delivery_date"] = payload.delivery_date

    if payload.items is not None:
        items_data = [item.model_dump() for item in payload.items]
        for item in items_data:
            item["line_total"] = round(item["unit_price"] * item["quantity"], 2)
        update_fields["items"] = items_data
        # Recalculate totals
        discounts = payload.discounts if payload.discounts is not None else doc.get("discounts", 0)
        transport_fee = payload.transport_fee if payload.transport_fee is not None else doc.get("transport_fee", 0)
        taxes = payload.taxes if payload.taxes is not None else doc.get("taxes", 0)
        totals = _calc_totals(items_data, discounts, transport_fee, taxes)
        update_fields.update(totals)
        update_fields["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2)

    if payload.discounts is not None and payload.items is None:
        update_fields["discounts"] = payload.discounts
        totals = _calc_totals(doc.get("items", []), payload.discounts, doc.get("transport_fee", 0), doc.get("taxes", 0))
        update_fields.update(totals)
        update_fields["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2)

    if payload.transport_fee is not None and payload.items is None:
        update_fields["transport_fee"] = payload.transport_fee
        totals = _calc_totals(doc.get("items", []), doc.get("discounts", 0), payload.transport_fee, doc.get("taxes", 0))
        update_fields.update(totals)
        update_fields["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2)

    if payload.taxes is not None and payload.items is None:
        update_fields["taxes"] = payload.taxes
        totals = _calc_totals(doc.get("items", []), doc.get("discounts", 0), doc.get("transport_fee", 0), payload.taxes)
        update_fields.update(totals)
        update_fields["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2)

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged.update(update_fields)
    encrypted = _enc(merged)
    await shop_bills_collection.update_one({"_id": raw_doc["_id"]}, {"$set": encrypted})

    await log_audit(
        current_user.get("user_name", ""),
        "update",
        "shop_bill",
        str(raw_doc["_id"]),
        details={"bill_number": doc.get("bill_number"), "changes": list(update_fields.keys())},
    )

    updated = await shop_bills_collection.find_one({"_id": raw_doc["_id"]})
    return _dec(updated)


@router.post("/{bill_id}/payment")
async def record_payment(
    bill_id: str,
    payload: ShopBillPayment,
    current_user: dict = Depends(require_capability("payment:write")),
):
    """Record a payment against a shop bill."""
    raw_doc = await shop_bills_collection.find_one({"_id": parse_object_id(bill_id, "Bill ID")})
    if not raw_doc:
        raw_doc = await shop_bills_collection.find_one({"bill_number": bill_id})
    if not raw_doc:
        raise HTTPException(status_code=404, detail="Shop bill not found")

    doc = _dec(raw_doc)
    now = datetime.now(timezone.utc)

    new_paid = doc.get("paid_amount", 0) + payload.amount
    grand_total = doc.get("grand_total", 0)
    outstanding = round(max(0, grand_total - new_paid), 2)

    if new_paid > grand_total + 0.01:
        raise HTTPException(status_code=400, detail="Payment exceeds outstanding amount")

    payment_status = "PAID" if outstanding <= 0.01 else "PARTIALLY_PAID"

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["paid_amount"] = round(new_paid, 2)
    merged["outstanding_amount"] = outstanding
    merged["payment_status"] = payment_status
    merged["updated_at"] = now
    if payment_status == "PAID":
        merged["status"] = "COMPLETED"

    encrypted = _enc(merged)
    await shop_bills_collection.update_one({"_id": raw_doc["_id"]}, {"$set": encrypted})

    await log_audit(
        current_user.get("user_name", ""),
        "payment",
        "shop_bill",
        str(raw_doc["_id"]),
        details={"bill_number": doc.get("bill_number"), "amount": payload.amount, "method": payload.payment_method},
    )

    updated = await shop_bills_collection.find_one({"_id": raw_doc["_id"]})
    return _dec(updated)


@router.delete("/{bill_id}")
async def delete_shop_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Delete a shop bill."""
    raw_doc = await shop_bills_collection.find_one({"_id": parse_object_id(bill_id, "Bill ID")})
    if not raw_doc:
        raw_doc = await shop_bills_collection.find_one({"bill_number": bill_id})
    if not raw_doc:
        raise HTTPException(status_code=404, detail="Shop bill not found")

    doc = _dec(raw_doc)
    await shop_bills_collection.delete_one({"_id": raw_doc["_id"]})

    await log_audit(
        current_user.get("user_name", ""),
        "delete",
        "shop_bill",
        str(raw_doc["_id"]),
        details={"bill_number": doc.get("bill_number")},
    )

    return {"message": "Shop bill deleted"}

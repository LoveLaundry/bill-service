import csv
import io
import random
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth_helper import require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import (
    audit_collection,
    bill_templates_collection,
    shop_bills_collection,
)
from ..models import (
    BillTemplateCreate,
    BillTemplateUpdate,
    ShopBillBulkStatus,
    ShopBillCreate,
    ShopBillModel,
    ShopBillPayment,
    ShopBillSplit,
    ShopBillMerge,
    ShopBillUpdate,
)
from ..repositories.main_repository import bump_version, enqueue_sync
from ..router_utils import log_audit, parse_object_id

router = APIRouter(prefix="/shop-bills", tags=["shop-bills"])

# ── Constants ─────────────────────────────────────────────────────────────────
SHOP_BILL_STATUSES = ["PENDING", "PROCESSING", "READY_FOR_DELIVERY", "OUT_FOR_DELIVERY", "DELIVERED", "CANCELLED"]
SHOP_PAYMENT_STATUSES = ["DRAFT", "PENDING", "PARTIALLY_PAID", "PAID", "OVERDUE", "REFUNDED"]
RECURRING_INTERVALS = ["DAILY", "WEEKLY", "BIWEEKLY", "MONTHLY"]

BILL_NUMBER_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
BILL_NUMBER_PREFIX = "SB-"

SENSITIVE_FIELDS = ["client_name", "notes", "items", "tags"]
TEMPLATE_SENSITIVE_FIELDS = ["client_name", "notes", "items"]


# ── Helper functions ──────────────────────────────────────────────────────────
def _enc(doc: dict) -> dict:
    """Encrypt a single shop bill document."""
    return encrypt_dict(doc, SENSITIVE_FIELDS)


def _dec(doc: dict) -> dict:
    """Decrypt a single shop bill document and normalize _id."""
    try:
        decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt document: {e}")
    if "_id" in decrypted:
        decrypted["id"] = str(decrypted["_id"])
        del decrypted["_id"]
    return decrypted


def _enc_template(doc: dict) -> dict:
    """Encrypt a bill template document."""
    return encrypt_dict(doc, TEMPLATE_SENSITIVE_FIELDS)


def _dec_template(doc: dict) -> dict:
    """Decrypt a bill template document and normalize _id."""
    try:
        decrypted = decrypt_dict(doc, TEMPLATE_SENSITIVE_FIELDS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt template: {e}")
    if "_id" in decrypted:
        decrypted["id"] = str(decrypted["_id"])
        del decrypted["_id"]
    return decrypted


async def _find_bill(bill_id: str) -> dict:
    """Find a shop bill by id (ObjectId or string). Raises 404 if missing."""
    oid = parse_object_id(bill_id, "Bill ID")
    doc = await shop_bills_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Shop bill not found")
    return doc


def _generate_bill_number() -> str:
    """Generate a unique confusable-safe bill number like SB-XXXXXXXX."""
    return BILL_NUMBER_PREFIX + "".join(random.choices(BILL_NUMBER_CHARS, k=8))


def _get_search_token(name: str) -> str:
    """Normalize client name for deterministic search queries."""
    return get_search_token(name)


def _calc_item_line_total(item: dict) -> float:
    """Compute line total for a single bill item."""
    unit_price = item.get("unit_price", 0)
    quantity = item.get("quantity", 0)
    discount = item.get("discount", 0) or 0
    discount_type = item.get("discount_type", "FIXED")

    raw = unit_price * quantity
    if discount_type == "PERCENT":
        discount_amt = round(raw * discount / 100, 2)
    else:
        discount_amt = min(discount, raw)
    return round(raw - discount_amt, 2)


def _prepare_items(items: list) -> None:
    """Sort items by category and assign positional indices (in-place)."""
    items.sort(key=lambda i: (i.get("category") or "", i.get("item_name") or ""))
    for idx, item in enumerate(items):
        item["position"] = idx
        item["line_total"] = _calc_item_line_total(item)


def _calc_totals(items: list, discounts: float = 0, transport_fee: float = 0, taxes: float = 0) -> dict:
    """Compute aggregate totals from items and optional fee/tax overrides."""
    total_quantity = sum(i.get("quantity", 0) for i in items)
    total_amount = round(sum(i.get("line_total", 0) for i in items), 2)
    grand_total = round(total_amount - (discounts or 0) + (transport_fee or 0) + (taxes or 0), 2)
    if grand_total < 0:
        grand_total = 0.0
    return {
        "total_quantity": total_quantity,
        "total_amount": total_amount,
        "grand_total": grand_total,
    }


def _build_bill_doc(body: dict, bill_number: str, now: datetime) -> dict:
    """Build a full shop bill document from a request body dict."""
    items = body.get("items", []) or []
    _prepare_items(items)
    discounts = body.get("discounts", 0) or 0
    transport_fee = body.get("transport_fee", 0) or 0
    taxes = body.get("taxes", 0) or 0
    totals = _calc_totals(items, discounts, transport_fee, taxes)
    grand_total = totals["grand_total"]

    return {
        "bill_number": bill_number,
        "client_name": body.get("client_name", ""),
        "quotation_id": body.get("quotation_id"),
        "items": items,
        "total_quantity": totals["total_quantity"],
        "total_amount": totals["total_amount"],
        "discounts": discounts,
        "transport_fee": transport_fee,
        "taxes": taxes,
        "grand_total": grand_total,
        "status": body.get("status", "PENDING"),
        "payment_status": body.get("payment_status", "DRAFT"),
        "paid_amount": body.get("paid_amount", 0) or 0,
        "outstanding_amount": grand_total - (body.get("paid_amount", 0) or 0),
        "notes": body.get("notes"),
        "notes_history": [],
        "delivery_date": body.get("delivery_date"),
        "tags": body.get("tags", []) or [],
        "locked": body.get("locked", False),
        "is_recurring": body.get("is_recurring", False),
        "recurring_interval": body.get("recurring_interval"),
        "recurring_end_date": body.get("recurring_end_date"),
        "parent_bill_id": body.get("parent_bill_id"),
        "created_at": now,
        "updated_at": now,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Features 1-50
# ══════════════════════════════════════════════════════════════════════════════

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_bill(
    payload: ShopBillCreate,
    current_user: dict = Depends(require_capability("bill:write")),
):
    now = datetime.now(timezone.utc)
    bill_number = payload.bill_number or _generate_bill_number()

    items = [i.model_dump() for i in payload.items]
    _prepare_items(items)
    discounts = payload.discounts or 0
    transport_fee = payload.transport_fee or 0
    taxes = payload.taxes or 0
    totals = _calc_totals(items, discounts, transport_fee, taxes)
    grand_total = totals["grand_total"]

    doc = {
        "bill_number": bill_number,
        "client_name": payload.client_name,
        "quotation_id": payload.quotation_id,
        "items": items,
        "total_quantity": totals["total_quantity"],
        "total_amount": totals["total_amount"],
        "discounts": discounts,
        "transport_fee": transport_fee,
        "taxes": taxes,
        "grand_total": grand_total,
        "status": "PENDING",
        "payment_status": "DRAFT",
        "paid_amount": 0.0,
        "outstanding_amount": grand_total,
        "notes": payload.notes,
        "notes_history": [],
        "delivery_date": payload.delivery_date,
        "tags": payload.tags or [],
        "locked": payload.locked or False,
        "is_recurring": payload.is_recurring or False,
        "recurring_interval": payload.recurring_interval,
        "recurring_end_date": payload.recurring_end_date,
        "parent_bill_id": None,
        "created_at": now,
        "updated_at": now,
    }

    result = await shop_bills_collection.insert_one(_enc(doc))
    doc["id"] = str(result.inserted_id)

    new_version = await bump_version("shop_bill", result.inserted_id)
    await enqueue_sync("shop_bill", result.inserted_id, new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_CREATE",
        "shop_bill",
        doc["id"],
    )
    return _dec(doc)


@router.get("")
async def list_bills(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=500),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: dict = Depends(require_capability("bill:read")),
):
    query: dict = {}
    if status_filter:
        query["status"] = status_filter

    total = await shop_bills_collection.count_documents(query)
    items = []
    async for doc in (
        shop_bills_collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
    ):
        items.append(_dec(doc))

    return {"items": items, "total": total}


@router.get("/{bill_id}")
async def get_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:read")),
):
    doc = await _find_bill(bill_id)
    return _dec(doc)


@router.patch("/{bill_id}")
async def update_bill(
    bill_id: str,
    payload: ShopBillUpdate,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)

    if doc.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot modify a locked bill")

    now = datetime.now(timezone.utc)
    update_fields: dict = {}

    for field in ("status", "payment_status", "notes", "delivery_date", "discounts", "transport_fee", "taxes", "tags", "locked"):
        val = getattr(payload, field)
        if val is not None:
            update_fields[field] = val

    if payload.items is not None:
        items = [i.model_dump() for i in payload.items]
        _prepare_items(items)
        update_fields["items"] = items

    existing_items = update_fields.get("items", doc.get("items", []))
    discounts = update_fields.get("discounts", doc.get("discounts", 0)) or 0
    transport_fee = update_fields.get("transport_fee", doc.get("transport_fee", 0)) or 0
    taxes = update_fields.get("taxes", doc.get("taxes", 0)) or 0
    totals = _calc_totals(existing_items, discounts, transport_fee, taxes)

    update_fields["total_quantity"] = totals["total_quantity"]
    update_fields["total_amount"] = totals["total_amount"]
    update_fields["grand_total"] = totals["grand_total"]
    update_fields["outstanding_amount"] = round(
        totals["grand_total"] - doc.get("paid_amount", 0), 2
    )
    update_fields["updated_at"] = now

    if not update_fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(update_fields)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_UPDATE",
        "shop_bill",
        str(raw["_id"]),
        details={"edited_fields": list(update_fields.keys())},
    )

    updated = await shop_bills_collection.find_one({"_id": raw["_id"]})
    return _dec(updated)


@router.delete("/{bill_id}")
async def delete_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)

    if doc.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot delete a locked bill")

    await shop_bills_collection.delete_one({"_id": raw["_id"]})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_DELETE",
        "shop_bill",
        str(raw["_id"]),
    )
    return {"message": "Bill deleted"}


@router.post("/{bill_id}/status")
async def change_status(
    bill_id: str,
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    new_status = body.get("status", "")
    if new_status not in SHOP_BILL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {SHOP_BILL_STATUSES}")

    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["status"] = new_status
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_STATUS_CHANGE",
        "shop_bill",
        str(raw["_id"]),
        details={"new_status": new_status},
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.post("/{bill_id}/lock")
async def lock_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    now = datetime.now(timezone.utc)

    await shop_bills_collection.update_one(
        {"_id": raw["_id"]},
        {"$set": {"locked": True, "updated_at": now}},
    )

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_LOCK",
        "shop_bill",
        str(raw["_id"]),
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.post("/{bill_id}/unlock")
async def unlock_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    now = datetime.now(timezone.utc)

    await shop_bills_collection.update_one(
        {"_id": raw["_id"]},
        {"$set": {"locked": False, "updated_at": now}},
    )

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_UNLOCK",
        "shop_bill",
        str(raw["_id"]),
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.post("/{bill_id}/record-payment")
async def record_payment(
    bill_id: str,
    payload: ShopBillPayment,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)

    if doc.get("payment_status") in ("PAID", "CANCELLED", "REFUNDED"):
        raise HTTPException(status_code=400, detail="Cannot record payment on this bill")

    outstanding = doc.get("outstanding_amount", 0) or 0
    if payload.amount > outstanding + 0.01:
        raise HTTPException(status_code=400, detail=f"Payment amount {payload.amount} exceeds outstanding {outstanding}")

    new_paid = round(doc.get("paid_amount", 0) + payload.amount, 2)
    new_outstanding = round(doc.get("grand_total", 0) - new_paid, 2)
    if new_outstanding < 0.01:
        new_outstanding = 0.0
        new_payment_status = "PAID"
    elif new_paid > 0:
        new_payment_status = "PARTIALLY_PAID"
    else:
        new_payment_status = doc.get("payment_status", "PENDING")

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["paid_amount"] = new_paid
    merged["outstanding_amount"] = new_outstanding
    merged["payment_status"] = new_payment_status
    merged["updated_at"] = now

    if not doc.get("payments"):
        merged["payments"] = []
    merged.setdefault("payments", []).append({
        "amount": payload.amount,
        "payment_method": payload.payment_method,
        "payment_date": payload.payment_date.isoformat() if isinstance(payload.payment_date, datetime) else str(payload.payment_date),
        "reference": payload.reference,
        "notes": payload.notes,
        "recorded_at": now.isoformat(),
    })

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_PAYMENT_RECORD",
        "shop_bill",
        str(raw["_id"]),
        details={"amount": payload.amount, "method": payload.payment_method},
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.post("/{bill_id}/split-bill")
async def split_bill(
    bill_id: str,
    payload: ShopBillSplit,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)

    if doc.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot split a locked bill")

    items = doc.get("items", [])
    indices = sorted(payload.item_indices, reverse=True)

    if any(i < 0 or i >= len(items) for i in indices):
        raise HTTPException(status_code=400, detail="Invalid item index")

    moved_items = []
    for idx in indices:
        moved_items.append(items.pop(idx))

    now = datetime.now(timezone.utc)

    remaining_totals = _calc_totals(items, doc.get("discounts", 0), doc.get("transport_fee", 0), doc.get("taxes", 0))
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["items"] = items
    merged["total_quantity"] = remaining_totals["total_quantity"]
    merged["total_amount"] = remaining_totals["total_amount"]
    merged["grand_total"] = remaining_totals["grand_total"]
    merged["outstanding_amount"] = round(remaining_totals["grand_total"] - doc.get("paid_amount", 0), 2)
    merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    moved_totals_data = _calc_totals(moved_items)
    moved_grand = moved_totals_data["grand_total"]
    new_bn = _generate_bill_number()

    new_doc = {
        "bill_number": new_bn,
        "client_name": doc.get("client_name", ""),
        "quotation_id": doc.get("quotation_id"),
        "items": moved_items,
        "total_quantity": moved_totals_data["total_quantity"],
        "total_amount": moved_totals_data["total_amount"],
        "discounts": 0,
        "transport_fee": 0,
        "taxes": 0,
        "grand_total": moved_grand,
        "status": "PENDING",
        "payment_status": "DRAFT",
        "paid_amount": 0,
        "outstanding_amount": moved_grand,
        "notes": f"Split from bill {doc.get('bill_number', '')}",
        "notes_history": [],
        "delivery_date": None,
        "tags": [],
        "locked": False,
        "is_recurring": False,
        "recurring_interval": None,
        "recurring_end_date": None,
        "parent_bill_id": str(raw["_id"]),
        "created_at": now,
        "updated_at": now,
    }

    result = await shop_bills_collection.insert_one(_enc(new_doc))
    new_doc["id"] = str(result.inserted_id)

    new_version = await bump_version("shop_bill", result.inserted_id)
    await enqueue_sync("shop_bill", result.inserted_id, new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_SPLIT",
        "shop_bill",
        str(raw["_id"]),
        details={"new_bill_number": new_bn, "items_moved": len(moved_items)},
    )
    return {"original": _dec(await shop_bills_collection.find_one({"_id": raw["_id"]})), "new_bill": _dec(new_doc)}


@router.post("/{bill_id}/merge-bills")
async def merge_bills(
    bill_id: str,
    payload: ShopBillMerge,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    base = _dec(raw)

    if base.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot merge into a locked bill")

    now = datetime.now(timezone.utc)
    all_items = list(base.get("items", []))
    merged_ids = [bill_id]

    for other_id in payload.bill_ids:
        if other_id == bill_id:
            continue
        other_raw = await _find_bill(other_id)
        other_doc = _dec(other_raw)

        if other_doc.get("locked"):
            raise HTTPException(status_code=400, detail=f"Bill {other_id} is locked")

        all_items.extend(other_doc.get("items", []))
        merged_ids.append(other_id)

    _prepare_items(all_items)
    totals = _calc_totals(all_items, base.get("discounts", 0), base.get("transport_fee", 0), base.get("taxes", 0))

    update = {k: v for k, v in base.items() if k not in ("id", "_id")}
    update["items"] = all_items
    update["total_quantity"] = totals["total_quantity"]
    update["total_amount"] = totals["total_amount"]
    update["grand_total"] = totals["grand_total"]
    update["outstanding_amount"] = round(totals["grand_total"] - base.get("paid_amount", 0), 2)
    update["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(update)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_MERGE",
        "shop_bill",
        str(raw["_id"]),
        details={"merged_ids": merged_ids},
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.post("/{bill_id}/duplicate")
async def duplicate_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    src = await _find_bill(bill_id)
    src_doc = _dec(src)
    now = datetime.now(timezone.utc)

    items = [i.copy() for i in src_doc.get("items", [])]
    _prepare_items(items)
    totals = _calc_totals(items)
    new_bn = _generate_bill_number()

    doc = {
        "bill_number": new_bn,
        "client_name": src_doc.get("client_name", ""),
        "quotation_id": src_doc.get("quotation_id"),
        "items": items,
        "total_quantity": totals["total_quantity"],
        "total_amount": totals["total_amount"],
        "discounts": 0,
        "transport_fee": 0,
        "taxes": 0,
        "grand_total": totals["grand_total"],
        "status": "PENDING",
        "payment_status": "DRAFT",
        "paid_amount": 0,
        "outstanding_amount": totals["grand_total"],
        "notes": f"Duplicated from {src_doc.get('bill_number', '')}",
        "notes_history": [],
        "delivery_date": None,
        "tags": [],
        "locked": False,
        "is_recurring": False,
        "recurring_interval": None,
        "recurring_end_date": None,
        "parent_bill_id": str(src["_id"]),
        "created_at": now,
        "updated_at": now,
    }

    result = await shop_bills_collection.insert_one(_enc(doc))
    doc["id"] = str(result.inserted_id)

    new_version = await bump_version("shop_bill", result.inserted_id)
    await enqueue_sync("shop_bill", result.inserted_id, new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_DUPLICATE",
        "shop_bill",
        doc["id"],
    )
    return _dec(doc)


@router.post("/{bill_id}/make-recurring")
async def make_recurring(
    bill_id: str,
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    interval = body.get("recurring_interval", "MONTHLY")
    if interval not in RECURRING_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Must be one of {RECURRING_INTERVALS}")

    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["is_recurring"] = True
    merged["recurring_interval"] = interval
    merged["recurring_end_date"] = body.get("recurring_end_date")
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_MAKE_RECURRING",
        "shop_bill",
        str(raw["_id"]),
        details={"interval": interval},
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.post("/{bill_id}/cancel-recurring")
async def cancel_recurring(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["is_recurring"] = False
    merged["recurring_interval"] = None
    merged["recurring_end_date"] = None
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_CANCEL_RECURRING",
        "shop_bill",
        str(raw["_id"]),
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.get("/recurring/due")
async def get_recurring_due(
    current_user: dict = Depends(require_capability("bill:read")),
):
    now = datetime.now(timezone.utc)
    dm = {
        "DAILY": timedelta(days=1),
        "WEEKLY": timedelta(weeks=1),
        "BIWEEKLY": timedelta(weeks=2),
        "MONTHLY": timedelta(days=30),
    }

    due = []
    async for raw in shop_bills_collection.find({"is_recurring": True}):
        doc = _dec(raw)
        end_date = doc.get("recurring_end_date")
        if end_date and isinstance(end_date, str):
            try:
                end_date = datetime.fromisoformat(end_date)
            except (ValueError, TypeError):
                end_date = None
        if end_date and now > end_date:
            continue

        interval = doc.get("recurring_interval", "MONTHLY")
        last_date = doc.get("updated_at") or doc.get("created_at", now)
        if isinstance(last_date, str):
            try:
                last_date = datetime.fromisoformat(last_date)
            except (ValueError, TypeError):
                last_date = now

        next_due = last_date + dm.get(interval, timedelta(days=30))
        if next_due <= now:
            due.append({
                "bill_number": doc.get("bill_number"),
                "client_name": doc.get("client_name"),
                "interval": interval,
                "grand_total": doc.get("grand_total", 0),
                "next_due": next_due.isoformat(),
            })

    return {"items": due, "total": len(due)}


@router.post("/quick-bill", status_code=status.HTTP_201_CREATED)
async def quick_bill(
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    now = datetime.now(timezone.utc)
    bn = _generate_bill_number()

    items = body.get("items", []) or []
    _prepare_items(items)
    totals = _calc_totals(items)
    grand_total = totals["grand_total"]

    doc = {
        "bill_number": bn,
        "client_name": body.get("client_name", ""),
        "quotation_id": None,
        "items": items,
        "total_quantity": totals["total_quantity"],
        "total_amount": totals["total_amount"],
        "discounts": 0,
        "transport_fee": 0,
        "taxes": 0,
        "grand_total": grand_total,
        "status": "PENDING",
        "payment_status": "DRAFT",
        "paid_amount": 0,
        "outstanding_amount": grand_total,
        "notes": body.get("notes"),
        "notes_history": [],
        "delivery_date": None,
        "tags": [],
        "locked": False,
        "is_recurring": False,
        "recurring_interval": None,
        "recurring_end_date": None,
        "parent_bill_id": None,
        "created_at": now,
        "updated_at": now,
    }

    result = await shop_bills_collection.insert_one(_enc(doc))
    doc["id"] = str(result.inserted_id)

    new_version = await bump_version("shop_bill", result.inserted_id)
    await enqueue_sync("shop_bill", result.inserted_id, new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_QUICK_CREATE",
        "shop_bill",
        doc["id"],
    )
    return _dec(doc)


@router.post("/manual-bill", status_code=status.HTTP_201_CREATED)
async def manual_bill(
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    now = datetime.now(timezone.utc)
    bn = _generate_bill_number()

    doc = _build_bill_doc(body, bn, now)
    doc["status"] = body.get("status", "PENDING")
    doc["payment_status"] = body.get("payment_status", "DRAFT")

    result = await shop_bills_collection.insert_one(_enc(doc))
    doc["id"] = str(result.inserted_id)

    new_version = await bump_version("shop_bill", result.inserted_id)
    await enqueue_sync("shop_bill", result.inserted_id, new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_MANUAL_CREATE",
        "shop_bill",
        doc["id"],
    )
    return _dec(doc)


@router.post("/{bill_id}/notes")
async def update_notes(
    bill_id: str,
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)
    new_notes = body.get("notes", "")

    history = doc.get("notes_history", [])
    history.append({
        "old_notes": doc.get("notes", ""),
        "new_notes": new_notes,
        "category": body.get("category", "general"),
        "changed_by": current_user.get("auth_id", "system"),
        "changed_at": now.isoformat(),
    })

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["notes"] = new_notes
    merged["notes_history"] = history
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_NOTES_UPDATE",
        "shop_bill",
        str(raw["_id"]),
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.get("/{bill_id}/notes-history")
async def get_notes_history(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:read")),
):
    doc = await _find_bill(bill_id)
    dec = _dec(doc)
    return {"notes_history": dec.get("notes_history", [])}


@router.post("/{bill_id}/tag")
async def add_tag(
    bill_id: str,
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)
    tag = body.get("tag", "").strip()

    if not tag:
        raise HTTPException(status_code=400, detail="Tag cannot be empty")

    tags = doc.get("tags", [])
    if tag not in tags:
        tags.append(tag)

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["tags"] = tags
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.delete("/{bill_id}/tag/{tag}")
async def remove_tag(
    bill_id: str,
    tag: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)

    tags = doc.get("tags", [])
    if tag in tags:
        tags.remove(tag)

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["tags"] = tags
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.post("/{bill_id}/item")
async def add_item(
    bill_id: str,
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    if doc.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot modify a locked bill")

    now = datetime.now(timezone.utc)
    items = doc.get("items", [])
    new_item = {
        "item_name": body.get("item_name", ""),
        "specification": body.get("specification"),
        "category": body.get("category"),
        "unit_price": body.get("unit_price", 0),
        "quantity": body.get("quantity", 1),
        "discount": body.get("discount", 0),
        "discount_type": body.get("discount_type", "FIXED"),
    }
    new_item["line_total"] = _calc_item_line_total(new_item)
    items.append(new_item)
    _prepare_items(items)

    totals = _calc_totals(items, doc.get("discounts", 0), doc.get("transport_fee", 0), doc.get("taxes", 0))
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["items"] = items
    merged.update(totals)
    merged["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2)
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.delete("/{bill_id}/item/{item_index}")
async def remove_item(
    bill_id: str,
    item_index: int,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    if doc.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot modify a locked bill")

    items = doc.get("items", [])
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(status_code=400, detail="Invalid item index")

    now = datetime.now(timezone.utc)
    items.pop(item_index)
    _prepare_items(items)

    totals = _calc_totals(items, doc.get("discounts", 0), doc.get("transport_fee", 0), doc.get("taxes", 0))
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["items"] = items
    merged.update(totals)
    merged["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2)
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.get("/stats/overview")
async def stats_overview(
    current_user: dict = Depends(require_capability("bill:read")),
):
    pipeline = [
        {
            "$group": {
                "_id": None,
                "total_bills": {"$sum": 1},
                "total_revenue": {"$sum": "$grand_total"},
                "total_paid": {"$sum": "$paid_amount"},
                "total_outstanding": {"$sum": "$outstanding_amount"},
            }
        }
    ]
    result = await shop_bills_collection.aggregate(pipeline).to_list(1)
    r = result[0] if result else {"total_bills": 0, "total_revenue": 0, "total_paid": 0, "total_outstanding": 0}

    status_counts = {}
    async for doc in shop_bills_collection.aggregate([{"$group": {"_id": "$status", "count": {"$sum": 1}}}]):
        status_counts[doc["_id"]] = doc["count"]

    return {
        "total_bills": r["total_bills"],
        "total_revenue": round(r["total_revenue"], 2),
        "total_paid": round(r["total_paid"], 2),
        "total_outstanding": round(r["total_outstanding"], 2),
        "status_counts": status_counts,
    }


@router.get("/stats/by-status")
async def stats_by_status(
    current_user: dict = Depends(require_capability("bill:read")),
):
    result = []
    async for doc in shop_bills_collection.aggregate([{"$group": {"_id": "$status", "count": {"$sum": 1}, "total": {"$sum": "$grand_total"}}}]):
        result.append({"status": doc["_id"], "count": doc["count"], "total": round(doc["total"], 2)})
    return {"items": result}


@router.get("/stats/by-client")
async def stats_by_client(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_capability("bill:read")),
):
    result = []
    pipeline = [
        {"$group": {"_id": "$client_name_search", "count": {"$sum": 1}, "total": {"$sum": "$grand_total"}, "paid": {"$sum": "$paid_amount"}}},
        {"$sort": {"total": -1}},
        {"$skip": skip},
        {"$limit": limit},
    ]
    async for doc in shop_bills_collection.aggregate(pipeline):
        result.append({
            "client_name_search": doc["_id"],
            "bill_count": doc["count"],
            "total_revenue": round(doc["total"], 2),
            "total_paid": round(doc["paid"], 2),
        })
    return {"items": result}


@router.get("/stats/revenue")
async def stats_revenue(
    current_user: dict = Depends(require_capability("bill:read")),
):
    pipeline = [
        {
            "$group": {
                "_id": {
                    "year": {"$year": "$created_at"},
                    "month": {"$month": "$created_at"},
                },
                "revenue": {"$sum": "$grand_total"},
                "paid": {"$sum": "$paid_amount"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"_id.year": -1, "_id.month": -1}},
        {"$limit": 12},
    ]
    result = []
    async for doc in shop_bills_collection.aggregate(pipeline):
        result.append({
            "year": doc["_id"]["year"],
            "month": doc["_id"]["month"],
            "revenue": round(doc["revenue"], 2),
            "paid": round(doc["paid"], 2),
            "bill_count": doc["count"],
        })
    return {"items": result}


@router.get("/stats/payment-summary")
async def payment_summary(
    current_user: dict = Depends(require_capability("bill:read")),
):
    pipeline = [
        {"$group": {"_id": "$payment_status", "count": {"$sum": 1}, "total": {"$sum": "$grand_total"}, "paid": {"$sum": "$paid_amount"}}}
    ]
    result = []
    async for doc in shop_bills_collection.aggregate(pipeline):
        result.append({
            "payment_status": doc["_id"],
            "count": doc["count"],
            "total": round(doc["total"], 2),
            "paid": round(doc["paid"], 2),
        })
    return {"items": result}


@router.get("/export/csv")
async def export_csv(
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: dict = Depends(require_capability("bill:read")),
):
    query = {"status": status_filter} if status_filter else {}
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bill Number", "Client", "Status", "Payment Status", "Grand Total", "Paid", "Outstanding", "Created At"])

    async for doc in shop_bills_collection.find(query).sort("created_at", -1):
        d = _dec(doc)
        writer.writerow([
            d.get("bill_number"),
            d.get("client_name"),
            d.get("status"),
            d.get("payment_status"),
            d.get("grand_total", 0),
            d.get("paid_amount", 0),
            d.get("outstanding_amount", 0),
            d.get("created_at"),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=shop-bills.csv"},
    )


@router.post("/export/csv-selected")
async def export_csv_selected(
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:read")),
):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bill Number", "Client", "Status", "Payment Status", "Grand Total", "Paid", "Outstanding"])

    for bid in body.get("bill_ids", []):
        raw = await shop_bills_collection.find_one({"_id": parse_object_id(bid, "Bill ID")})
        if raw:
            d = _dec(raw)
            writer.writerow([
                d.get("bill_number"),
                d.get("client_name"),
                d.get("status"),
                d.get("payment_status"),
                d.get("grand_total", 0),
                d.get("paid_amount", 0),
                d.get("outstanding_amount", 0),
            ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=selected-bills.csv"},
    )


@router.get("/overdue")
async def list_overdue(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_capability("bill:read")),
):
    query = {"payment_status": {"$in": ["PENDING", "PARTIALLY_PAID", "OVERDUE"]}, "outstanding_amount": {"$gt": 0}}
    total = await shop_bills_collection.count_documents(query)
    items = []
    async for doc in shop_bills_collection.find(query).sort("created_at", -1).skip(skip).limit(limit):
        items.append(_dec(doc))
    return {"items": items, "total": total}


@router.post("/bulk-status")
async def bulk_status(
    payload: ShopBillBulkStatus,
    current_user: dict = Depends(require_capability("bill:write")),
):
    if payload.status not in SHOP_BILL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")

    now = datetime.now(timezone.utc)
    updated = 0

    for bid in payload.bill_ids:
        raw = await shop_bills_collection.find_one({"_id": parse_object_id(bid, "Bill ID")})
        if not raw:
            continue
        doc = _dec(raw)
        if doc.get("locked"):
            continue

        merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
        merged["status"] = payload.status
        merged["updated_at"] = now

        await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
        vi = await bump_version("shop_bill", raw["_id"])
        await enqueue_sync("shop_bill", raw["_id"], vi)
        updated += 1

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_BULK_STATUS",
        "shop_bill",
        "multiple",
        details={"count": updated, "status": payload.status},
    )
    return {"updated": updated}


@router.post("/bulk-delete")
async def bulk_delete(
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    bill_ids = body.get("bill_ids", [])
    now = datetime.now(timezone.utc)
    deleted = 0

    for bid in bill_ids:
        raw = await shop_bills_collection.find_one({"_id": parse_object_id(bid, "Bill ID")})
        if not raw:
            continue
        doc = _dec(raw)
        if doc.get("locked"):
            continue

        await shop_bills_collection.delete_one({"_id": raw["_id"]})
        vi = await bump_version("shop_bill", raw["_id"])
        await enqueue_sync("shop_bill", raw["_id"], vi)
        deleted += 1

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_BULK_DELETE",
        "shop_bill",
        "multiple",
        details={"count": deleted},
    )
    return {"deleted": deleted}


@router.post("/{bill_id}/mark-delivered")
async def mark_delivered(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["status"] = "DELIVERED"
    merged["updated_at"] = now

    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})

    new_version = await bump_version("shop_bill", raw["_id"])
    await enqueue_sync("shop_bill", raw["_id"], new_version)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_MARK_DELIVERED",
        "shop_bill",
        str(raw["_id"]),
    )
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))


@router.get("/clients/{client_name}")
async def bills_by_client(
    client_name: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_capability("bill:read")),
):
    token = _get_search_token(client_name)
    query = {"client_name_search": token}
    total = await shop_bills_collection.count_documents(query)
    items = []
    async for doc in shop_bills_collection.find(query).sort("created_at", -1).skip(skip).limit(limit):
        items.append(_dec(doc))
    return {"items": items, "total": total}


@router.get("/clients/{client_name}/statement")
async def client_statement(
    client_name: str,
    current_user: dict = Depends(require_capability("bill:read")),
):
    token = _get_search_token(client_name)
    total_bills = 0
    total_revenue = 0.0
    total_paid = 0.0
    bills = []

    async for doc in shop_bills_collection.find({"client_name_search": token}).sort("created_at", -1):
        d = _dec(doc)
        total_bills += 1
        total_revenue += d.get("grand_total", 0)
        total_paid += d.get("paid_amount", 0)
        bills.append(d)

    return {
        "client_name": client_name,
        "total_bills": total_bills,
        "total_revenue": round(total_revenue, 2),
        "total_paid": round(total_paid, 2),
        "total_outstanding": round(total_revenue - total_paid, 2),
        "bills": bills,
    }


@router.get("/audit-trail/{bill_id}")
async def audit_trail(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:read")),
):
    logs = []
    async for doc in audit_collection.find({"entity_id": bill_id}).sort("timestamp", -1).limit(100):
        doc["id"] = str(doc.pop("_id", ""))
        logs.append(doc)
    return {"items": logs, "total": len(logs)}


@router.post("/search")
async def advanced_search(
    body: dict = Body(...),
    current_user: dict = Depends(require_capability("bill:read")),
):
    q = body.get("query", "")
    filters = body.get("filters", {})
    query: dict = {}

    if q:
        query["$or"] = [
            {"bill_number": {"$regex": q, "$options": "i"}},
            {"client_name_search": {"$regex": q, "$options": "i"}},
            {"notes": {"$regex": q, "$options": "i"}},
        ]

    for f in ("status", "payment_status"):
        if filters.get(f):
            query[f] = filters[f]

    items = []
    async for doc in shop_bills_collection.find(query).sort("created_at", -1).limit(body.get("limit", 50)):
        items.append(_dec(doc))
    return {"items": items, "total": len(items)}


@router.get("/recent-activity")
async def recent_activity(
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_capability("bill:read")),
):
    items = []
    async for doc in shop_bills_collection.find().sort("updated_at", -1).limit(limit):
        d = _dec(doc)
        items.append({
            "id": d.get("id"),
            "bill_number": d.get("bill_number"),
            "client_name": d.get("client_name"),
            "status": d.get("status"),
            "grand_total": d.get("grand_total"),
            "updated_at": d.get("updated_at"),
        })
    return {"items": items}


@router.get("/dashboard/summary")
async def dashboard_summary(
    current_user: dict = Depends(require_capability("bill:read")),
):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total_bills = await shop_bills_collection.count_documents({})

    today_count = await shop_bills_collection.count_documents({"created_at": {"$gte": today_start}})
    today_revenue = 0.0
    async for doc in shop_bills_collection.find({"created_at": {"$gte": today_start}}):
        today_revenue += _dec(doc).get("grand_total", 0)

    week_count = await shop_bills_collection.count_documents({"created_at": {"$gte": week_start}})
    week_revenue = 0.0
    async for doc in shop_bills_collection.find({"created_at": {"$gte": week_start}}):
        week_revenue += _dec(doc).get("grand_total", 0)

    month_count = await shop_bills_collection.count_documents({"created_at": {"$gte": month_start}})
    month_revenue = 0.0
    async for doc in shop_bills_collection.find({"created_at": {"$gte": month_start}}):
        month_revenue += _dec(doc).get("grand_total", 0)

    outstanding = 0.0
    overdue_count = 0
    async for doc in shop_bills_collection.find({"outstanding_amount": {"$gt": 0}, "status": {"$ne": "CANCELLED"}}):
        d = _dec(doc)
        outstanding += d.get("outstanding_amount", 0)
        if d.get("payment_status") in ("OVERDUE", "PENDING"):
            overdue_count += 1

    return {
        "total_bills": total_bills,
        "today": {"count": today_count, "revenue": round(today_revenue, 2)},
        "this_week": {"count": week_count, "revenue": round(week_revenue, 2)},
        "this_month": {"count": month_count, "revenue": round(month_revenue, 2)},
        "outstanding": round(outstanding, 2),
        "overdue_count": overdue_count,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Features 51-100
# ══════════════════════════════════════════════════════════════════════════════

@router.patch("/{bill_id}/transport-fee")
async def update_transport_fee(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw)
    if doc.get("locked"): raise HTTPException(status_code=400, detail="Cannot modify a locked bill")
    now = datetime.now(timezone.utc); new_fee = body.get("transport_fee", 0)
    totals = _calc_totals(doc.get("items", []), doc.get("discounts", 0), new_fee, doc.get("taxes", 0))
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["transport_fee"] = new_fee; merged.update(totals); merged["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2); merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.patch("/{bill_id}/taxes")
async def update_taxes(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw)
    if doc.get("locked"): raise HTTPException(status_code=400, detail="Cannot modify a locked bill")
    now = datetime.now(timezone.utc); new_taxes = body.get("taxes", 0)
    totals = _calc_totals(doc.get("items", []), doc.get("discounts", 0), doc.get("transport_fee", 0), new_taxes)
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["taxes"] = new_taxes; merged.update(totals); merged["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2); merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.patch("/{bill_id}/items/{item_index}")
async def update_item(bill_id: str, item_index: int, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw)
    if doc.get("locked"): raise HTTPException(status_code=400, detail="Cannot modify a locked bill")
    items = doc.get("items", [])
    if item_index < 0 or item_index >= len(items): raise HTTPException(status_code=400, detail="Invalid item index")
    now = datetime.now(timezone.utc); item = items[item_index]
    for f in ("item_name", "specification", "category", "unit_price", "quantity", "discount", "discount_type"):
        if f in body: item[f] = body[f]
    item["line_total"] = _calc_item_line_total(item); _prepare_items(items)
    totals = _calc_totals(items, doc.get("discounts", 0), doc.get("transport_fee", 0), doc.get("taxes", 0))
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["items"] = items; merged.update(totals); merged["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2); merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.patch("/{bill_id}/items/reorder")
async def reorder_items(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw)
    if doc.get("locked"): raise HTTPException(status_code=400, detail="Cannot modify a locked bill")
    items = doc.get("items", []); order = body.get("order", [])
    if len(order) != len(items): raise HTTPException(status_code=400, detail="Order must match items")
    reordered = [items[i] for i in order if 0 <= i < len(items)]
    now = datetime.now(timezone.utc); _prepare_items(reordered)
    totals = _calc_totals(reordered, doc.get("discounts", 0), doc.get("transport_fee", 0), doc.get("taxes", 0))
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["items"] = reordered; merged.update(totals); merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.patch("/{bill_id}/link-quotation")
async def link_quotation(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw); now = datetime.now(timezone.utc)
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["quotation_id"] = body.get("quotation_id"); merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.delete("/{bill_id}/link-quotation")
async def unlink_quotation(bill_id: str, current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw); now = datetime.now(timezone.utc)
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["quotation_id"] = None; merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.post("/bulk-status-notes")
async def bulk_status_notes(body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    bill_ids, status_val, notes = body.get("bill_ids", []), body.get("status"), body.get("notes", "")
    if not bill_ids or not status_val: raise HTTPException(status_code=400, detail="bill_ids and status required")
    if status_val not in SHOP_BILL_STATUSES: raise HTTPException(status_code=400, detail=f"Invalid status: {status_val}")
    now = datetime.now(timezone.utc); updated = 0
    for bid in bill_ids:
        raw = await shop_bills_collection.find_one({"_id": parse_object_id(bid, "Bill ID")})
        if not raw: continue
        doc = _dec(raw)
        if doc.get("locked"): continue
        merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
        merged["status"] = status_val
        if notes: merged["notes"] = (merged.get("notes") or "") + f"\n[STATUS] {notes}"
        merged["updated_at"] = now
        await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
        vi = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], vi); updated += 1
    await log_audit(current_user.get("auth_id", "system"), "bulk_status_notes", "shop_bill", "multiple", details={"count": updated, "status": status_val})
    return {"updated": updated}

@router.post("/{bill_id}/duplicate-options")
async def dup_opts(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); src = _dec(raw); now = datetime.now(timezone.utc)
    items = [i.copy() for i in src.get("items", [])] if body.get("keep_items", True) else []
    bn = _generate_bill_number(); gt = sum(i.get("line_total", 0) for i in items)
    doc = {"bill_number": bn, "client_name": body.get("client_name", src.get("client_name")), "quotation_id": src.get("quotation_id"), "items": items, "total_quantity": sum(i.get("quantity", 0) for i in items), "total_amount": gt, "discounts": 0, "transport_fee": 0, "taxes": 0, "grand_total": gt, "status": "PENDING", "payment_status": "DRAFT", "paid_amount": 0, "outstanding_amount": gt, "notes": None, "notes_history": [], "delivery_date": None, "tags": [], "locked": False, "is_recurring": False, "recurring_interval": None, "recurring_end_date": None, "parent_bill_id": src["id"], "created_at": now, "updated_at": now}
    result = await shop_bills_collection.insert_one(_enc(doc)); doc["id"] = str(result.inserted_id)
    v = await bump_version("shop_bill", result.inserted_id); await enqueue_sync("shop_bill", result.inserted_id, v)
    return _dec(doc)

@router.post("/fulltext-search")
async def fulltext(body: dict = Body(...), current_user: dict = Depends(require_capability("bill:read"))):
    q = body.get("q", "")
    if not q: return {"items": [], "total": 0}
    items = []
    async for doc in shop_bills_collection.find({"$or": [{"bill_number": {"$regex": q, "$options": "i"}}, {"client_name": {"$regex": q, "$options": "i"}}, {"notes": {"$regex": q, "$options": "i"}}]}).limit(body.get("limit", 50)):
        items.append(_dec(doc))
    return {"items": items, "total": len(items)}

@router.get("/tags")
async def all_tags(current_user: dict = Depends(require_capability("bill:read"))):
    all_t: set = set()
    async for doc in shop_bills_collection.find({"tags": {"$exists": True, "$ne": []}}, {"tags": 1}):
        for t in doc.get("tags", []): all_t.add(t)
    return {"tags": sorted(all_t)}

@router.get("/by-tag/{tag}")
async def by_tag(tag: str, skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), current_user: dict = Depends(require_capability("bill:read"))):
    total = await shop_bills_collection.count_documents({"tags": tag})
    items = []
    async for doc in shop_bills_collection.find({"tags": tag}).sort("created_at", -1).skip(skip).limit(limit):
        items.append(_dec(doc))
    return {"items": items, "total": total}

@router.post("/{bill_id}/notes-category")
async def note_cat(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw); now = datetime.now(timezone.utc)
    history = doc.get("notes_history", [])
    history.append({"old_notes": doc.get("notes", ""), "new_notes": body.get("note", ""), "category": body.get("category", "general"), "changed_by": current_user.get("auth_id", "system"), "changed_at": now.isoformat()})
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["notes"] = body.get("note", ""); merged["notes_history"] = history; merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.post("/from-quotation/{quotation_id}", status_code=201)
async def from_quotation(quotation_id: str, current_user: dict = Depends(require_capability("bill:write"))):
    from ..database.main_db import quotations_collection
    from ..crypto_helper import decrypt_dict as _dd
    q_raw = await quotations_collection.find_one({"_id": parse_object_id(quotation_id, "Quotation ID")})
    if not q_raw: raise HTTPException(status_code=404, detail="Quotation not found")
    try: q_doc = _dd(q_raw, ["client_name", "items", "notes"])
    except Exception: q_doc = q_raw
    now = datetime.now(timezone.utc)
    items_data = [{"item_name": qi.get("item_name", ""), "specification": qi.get("specification"), "category": qi.get("category"), "unit_price": qi.get("unit_price", 0), "quantity": qi.get("quantity", 1), "discount": 0, "discount_type": "FIXED"} for qi in q_doc.get("items", [])]
    _prepare_items(items_data); totals = _calc_totals(items_data); bn = _generate_bill_number()
    doc = {"bill_number": bn, "client_name": q_doc.get("client_name", ""), "quotation_id": quotation_id, "items": items_data, "total_quantity": totals["total_quantity"], "total_amount": totals["total_amount"], "discounts": 0, "transport_fee": 0, "taxes": 0, "grand_total": totals["grand_total"], "status": "PENDING", "payment_status": "DRAFT", "paid_amount": 0, "outstanding_amount": totals["grand_total"], "notes": q_doc.get("notes"), "notes_history": [], "delivery_date": None, "tags": [], "locked": False, "is_recurring": False, "recurring_interval": None, "recurring_end_date": None, "parent_bill_id": None, "created_at": now, "updated_at": now}
    result = await shop_bills_collection.insert_one(_enc(doc)); doc["id"] = str(result.inserted_id)
    v = await bump_version("shop_bill", result.inserted_id); await enqueue_sync("shop_bill", result.inserted_id, v)
    return _dec(doc)

@router.get("/stats/daily")
async def daily_summary(date: Optional[str] = Query(None), current_user: dict = Depends(require_capability("bill:read"))):
    now = datetime.now(timezone.utc); target = datetime.fromisoformat(date) if date else now
    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    result = await shop_bills_collection.aggregate([{"$match": {"created_at": {"$gte": start, "$lt": start + timedelta(days=1)}}}, {"$group": {"_id": "$status", "count": {"$sum": 1}, "total": {"$sum": "$grand_total"}}}]).to_list(length=20)
    return {"date": start.strftime("%Y-%m-%d"), "total_bills": sum(r["count"] for r in result), "total_revenue": round(sum(r["total"] for r in result), 2)}

@router.get("/stats/weekly")
async def weekly_summary(current_user: dict = Depends(require_capability("bill:read"))):
    now = datetime.now(timezone.utc); start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await shop_bills_collection.aggregate([{"$match": {"created_at": {"$gte": start, "$lt": start + timedelta(days=7)}}}, {"$group": {"_id": {"day": {"$dayOfWeek": "$created_at"}}, "count": {"$sum": 1}, "total": {"$sum": "$grand_total"}}}]).to_list(length=7)
    return {"week_start": start.strftime("%Y-%m-%d"), "total_bills": sum(r["count"] for r in result), "total_revenue": round(sum(r["total"] for r in result), 2)}

@router.get("/stats/monthly")
async def monthly_summary(year: Optional[int] = Query(None), month: Optional[int] = Query(None), current_user: dict = Depends(require_capability("bill:read"))):
    now = datetime.now(timezone.utc); y, m = year or now.year, month or now.month
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + 1, 1, 1, tzinfo=timezone.utc) if m == 12 else datetime(y, m + 1, 1, tzinfo=timezone.utc)
    result = await shop_bills_collection.aggregate([{"$match": {"created_at": {"$gte": start, "$lt": end}}}, {"$group": {"_id": None, "count": {"$sum": 1}, "revenue": {"$sum": "$grand_total"}, "paid": {"$sum": "$paid_amount"}}}]).to_list(length=1)
    r = result[0] if result else {"count": 0, "revenue": 0, "paid": 0}
    return {"year": y, "month": m, "total_bills": r["count"], "total_revenue": round(r["revenue"], 2), "total_paid": round(r["paid"], 2), "outstanding": round(r["revenue"] - r["paid"], 2)}

@router.get("/stats/yearly")
async def yearly_summary(year: Optional[int] = Query(None), current_user: dict = Depends(require_capability("bill:read"))):
    y = year or datetime.now(timezone.utc).year; start, end = datetime(y, 1, 1, tzinfo=timezone.utc), datetime(y + 1, 1, 1, tzinfo=timezone.utc)
    result = await shop_bills_collection.aggregate([{"$match": {"created_at": {"$gte": start, "$lt": end}}}, {"$group": {"_id": {"month": {"$month": "$created_at"}}, "count": {"$sum": 1}, "revenue": {"$sum": "$grand_total"}, "paid": {"$sum": "$paid_amount"}}}]).to_list(length=12)
    total, rev, paid = sum(r["count"] for r in result), sum(r["revenue"] for r in result), sum(r["paid"] for r in result)
    return {"year": y, "total_bills": total, "total_revenue": round(rev, 2), "total_paid": round(paid, 2), "outstanding": round(rev - paid, 2)}

@router.get("/client-loyalty/{client_name}")
async def loyalty(client_name: str, current_user: dict = Depends(require_capability("bill:read"))):
    token = get_search_token(client_name); total_bills, total_spent = 0, 0.0; fb = lb = None
    async for raw in shop_bills_collection.find({"client_name_search": token}):
        doc = _dec(raw); total_bills += 1; total_spent += doc.get("grand_total", 0); dt = doc.get("created_at")
        if dt:
            if fb is None or dt < fb: fb = dt
            if lb is None or dt > lb: lb = dt
    return {"client_name": client_name, "total_bills": total_bills, "total_spent": round(total_spent, 2), "first_bill_date": fb, "last_bill_date": lb, "avg_bill": round(total_spent / total_bills, 2) if total_bills > 0 else 0}

@router.get("/templates/{template_id}/usage")
async def tpl_usage(template_id: str, current_user: dict = Depends(require_capability("bill:read"))):
    raw = await bill_templates_collection.find_one({"_id": parse_object_id(template_id, "Template ID")})
    if not raw: raise HTTPException(status_code=404, detail="Template not found")
    doc = _dec_template(raw)
    return {"template_id": template_id, "name": doc.get("name"), "use_count": doc.get("use_count", 0)}

@router.post("/templates/{template_id}/duplicate", status_code=201)
async def dup_template(template_id: str, current_user: dict = Depends(require_capability("bill:write"))):
    raw = await bill_templates_collection.find_one({"_id": parse_object_id(template_id, "Template ID")})
    if not raw: raise HTTPException(status_code=404, detail="Template not found")
    src = _dec_template(raw); now = datetime.now(timezone.utc)
    doc = {"name": f"{src['name']} (Copy)", "client_name": src.get("client_name"), "items": [i.copy() for i in src.get("items", [])], "discounts": src.get("discounts", 0), "transport_fee": src.get("transport_fee", 0), "taxes": src.get("taxes", 0), "notes": src.get("notes"), "use_count": 0, "created_at": now, "updated_at": now}
    result = await bill_templates_collection.insert_one(_enc_template(doc)); doc["id"] = str(result.inserted_id)
    v = await bump_version("bill_template", result.inserted_id); await enqueue_sync("bill_template", result.inserted_id, v)
    return _dec_template(doc)

@router.patch("/templates/{template_id}")
async def update_tpl(template_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await bill_templates_collection.find_one({"_id": parse_object_id(template_id, "Template ID")})
    if not raw: raise HTTPException(status_code=404, detail="Template not found")
    doc = _dec_template(raw); now = datetime.now(timezone.utc)
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    for f in ("name", "client_name", "items", "discounts", "transport_fee", "taxes", "notes"):
        if f in body: merged[f] = body[f]
    merged["updated_at"] = now
    await bill_templates_collection.update_one({"_id": raw["_id"]}, {"$set": _enc_template(merged)})
    v = await bump_version("bill_template", raw["_id"]); await enqueue_sync("bill_template", raw["_id"], v)
    return _dec_template(await bill_templates_collection.find_one({"_id": raw["_id"]}))

@router.post("/{bill_id}/attachments")
async def add_att(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw); now = datetime.now(timezone.utc)
    atts = doc.get("attachments", []); atts.append({"name": body.get("name", ""), "url": body.get("url", ""), "added_at": now.isoformat()})
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}; merged["attachments"] = atts; merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.get("/{bill_id}/attachments")
async def get_att(bill_id: str, current_user: dict = Depends(require_capability("bill:read"))):
    return {"attachments": _dec(await _find_bill(bill_id)).get("attachments", [])}

@router.delete("/{bill_id}/attachments/{index}")
async def del_att(bill_id: str, index: int, current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw); atts = doc.get("attachments", [])
    if index < 0 or index >= len(atts): raise HTTPException(status_code=400, detail="Invalid index")
    now = datetime.now(timezone.utc); atts.pop(index)
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}; merged["attachments"] = atts; merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return {"message": "Attachment deleted"}

@router.get("/{bill_id}/versions")
async def versions(bill_id: str, current_user: dict = Depends(require_capability("bill:read"))):
    from ..database.main_db import versions_collection
    vs = []
    async for doc in versions_collection.find({"entity_type": "shop_bill", "entity_id": bill_id}).sort("version", -1).limit(20):
        vs.append({"version": doc.get("version"), "timestamp": doc.get("timestamp"), "user": doc.get("user")})
    return {"versions": vs}

@router.get("/{bill_id}/print-data")
async def print_data(bill_id: str, current_user: dict = Depends(require_capability("bill:read"))):
    doc = _dec(await _find_bill(bill_id))
    return {k: doc.get(k) for k in ("bill_number", "client_name", "delivery_date", "items", "total_amount", "discounts", "transport_fee", "taxes", "grand_total", "paid_amount", "outstanding_amount", "notes", "created_at")}

@router.post("/bulk-print-data")
async def bulk_print(body: dict = Body(...), current_user: dict = Depends(require_capability("bill:read"))):
    bills = []
    for bid in body.get("bill_ids", [])[:10]:
        raw = await shop_bills_collection.find_one({"_id": parse_object_id(bid, "Bill ID")})
        if raw: d = _dec(raw); bills.append({"bill_number": d.get("bill_number"), "client_name": d.get("client_name"), "items": d.get("items", []), "grand_total": d.get("grand_total", 0)})
    return {"bills": bills}

@router.post("/calculate-tax")
async def calc_tax(body: dict = Body(...), current_user: dict = Depends(require_capability("bill:read"))):
    sub, rate = body.get("subtotal", 0), body.get("tax_rate", 0); amt = round(sub * rate / 100, 2)
    return {"subtotal": sub, "tax_rate": rate, "tax_amount": amt, "total": round(sub + amt, 2)}

@router.post("/calculate-discount")
async def calc_disc(body: dict = Body(...), current_user: dict = Depends(require_capability("bill:read"))):
    sub, disc, dt = body.get("subtotal", 0), body.get("discount", 0), body.get("discount_type", "FIXED")
    amt = round(sub * disc / 100, 2) if dt == "PERCENT" else disc
    return {"subtotal": sub, "discount": disc, "discount_type": dt, "discount_amount": amt, "total": round(sub - amt, 2)}

@router.post("/batch-create", status_code=201)
async def batch(body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    now = datetime.now(timezone.utc); created = []
    for bd in body.get("bills", [])[:20]:
        bn = bd.get("bill_number") or _generate_bill_number(); doc = _build_bill_doc(bd, bn, now)
        result = await shop_bills_collection.insert_one(_enc(doc)); doc["id"] = str(result.inserted_id)
        v = await bump_version("shop_bill", result.inserted_id); await enqueue_sync("shop_bill", result.inserted_id, v)
        created.append({"bill_number": bn, "id": doc["id"]})
    return {"created": len(created), "bills": created}

@router.get("/lookup")
async def lookup(q: str = Query(..., min_length=1), current_user: dict = Depends(require_capability("bill:read"))):
    items = []
    async for doc in shop_bills_collection.find({"$or": [{"bill_number": {"$regex": q, "$options": "i"}}, {"client_name_search": {"$regex": q, "$options": "i"}}]}).limit(10):
        d = _dec(doc); items.append({"id": d.get("id"), "bill_number": d.get("bill_number"), "client_name": d.get("client_name"), "grand_total": d.get("grand_total", 0)})
    return {"items": items}

@router.get("/client-count/{client_name}")
async def client_cnt(client_name: str, current_user: dict = Depends(require_capability("bill:read"))):
    count = await shop_bills_collection.count_documents({"client_name_search": get_search_token(client_name)})
    return {"client_name": client_name, "bill_count": count}

@router.get("/today")
async def bills_today(current_user: dict = Depends(require_capability("bill:read"))):
    now = datetime.now(timezone.utc); start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    items = []
    async for doc in shop_bills_collection.find({"created_at": {"$gte": start, "$lt": start + timedelta(days=1)}}).sort("created_at", -1): items.append(_dec(doc))
    return {"items": items, "total": len(items)}

@router.get("/this-week")
async def bills_this_week(current_user: dict = Depends(require_capability("bill:read"))):
    now = datetime.now(timezone.utc); start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    items = []
    async for doc in shop_bills_collection.find({"created_at": {"$gte": start}}).sort("created_at", -1): items.append(_dec(doc))
    return {"items": items, "total": len(items)}

@router.get("/this-month")
async def bills_this_month(current_user: dict = Depends(require_capability("bill:read"))):
    now = datetime.now(timezone.utc); start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    items = []
    async for doc in shop_bills_collection.find({"created_at": {"$gte": start}}).sort("created_at", -1): items.append(_dec(doc))
    return {"items": items, "total": len(items)}

@router.get("/stats/client-turnaround")
async def turnaround(current_user: dict = Depends(require_capability("bill:read"))):
    ts = []
    async for raw in shop_bills_collection.find({"delivery_date": {"$exists": True, "$ne": None}}):
        doc = _dec(raw); c, d = doc.get("created_at"), doc.get("delivery_date")
        if c and d:
            try:
                if isinstance(d, str): d = datetime.fromisoformat(d)
                delta = (d - c).days
                if delta >= 0: ts.append(delta)
            except (ValueError, TypeError): pass
    return {"avg_days": round(sum(ts) / len(ts), 1) if ts else 0, "sample_size": len(ts)}

@router.get("/stats/payment-methods")
async def pay_methods(current_user: dict = Depends(require_capability("bill:read"))):
    return {"message": "Payment method tracking available via audit logs"}

@router.get("/export/json")
async def json_export(status: Optional[str] = Query(None), current_user: dict = Depends(require_capability("bill:read"))):
    query = {"status": status} if status else {}; items = []
    async for doc in shop_bills_collection.find(query).sort("created_at", -1).limit(1000): items.append(_dec(doc))
    return {"bills": items, "total": len(items), "exported_at": datetime.now(timezone.utc).isoformat()}

@router.post("/diff")
async def bill_diff(body: dict = Body(...), current_user: dict = Depends(require_capability("bill:read"))):
    d1 = _dec(await _find_bill(body.get("bill_id_1"))); d2 = _dec(await _find_bill(body.get("bill_id_2")))
    diff = {}
    for key in ("client_name", "grand_total", "total_amount", "discounts", "transport_fee", "taxes", "status", "payment_status"):
        v1, v2 = d1.get(key), d2.get(key)
        if v1 != v2: diff[key] = {"bill_1": v1, "bill_2": v2}
    diff["items_count"] = {"bill_1": len(d1.get("items", [])), "bill_2": len(d2.get("items", []))}
    return {"bill_1": d1.get("bill_number"), "bill_2": d2.get("bill_number"), "diff": diff}

@router.get("/health")
async def health_check():
    return {"status": "ok", "bills_count": await shop_bills_collection.count_documents({}), "templates_count": await bill_templates_collection.count_documents({})}

@router.get("/schema")
async def schema_info():
    return {"bill_statuses": SHOP_BILL_STATUSES, "payment_statuses": SHOP_PAYMENT_STATUSES, "recurring_intervals": RECURRING_INTERVALS, "discount_types": ["FIXED", "PERCENT"]}

@router.post("/export/csv-by-ids")
async def csv_ids(body: dict = Body(...), current_user: dict = Depends(require_capability("bill:read"))):
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(["Bill Number", "Client", "Grand Total", "Paid", "Outstanding", "Status"])
    for bid in body.get("bill_ids", []):
        raw = await shop_bills_collection.find_one({"_id": parse_object_id(bid, "Bill ID")})
        if raw: d = _dec(raw); writer.writerow([d.get("bill_number"), d.get("client_name"), d.get("grand_total", 0), d.get("paid_amount", 0), d.get("outstanding_amount", 0), d.get("status")])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=selected-bills.csv"})

@router.get("/stats/payment-projection")
async def projection(days: int = Query(30, ge=1, le=90), current_user: dict = Depends(require_capability("bill:read"))):
    total = 0.0
    async for raw in shop_bills_collection.find({"outstanding_amount": {"$gt": 0}, "status": {"$ne": "CANCELLED"}}): total += _dec(raw).get("outstanding_amount", 0)
    return {"total_outstanding": round(total, 2), "projected_days": days, "avg_daily_collection": round(total / days, 2) if days > 0 else 0}

@router.post("/{bill_id}/refund")
async def refund(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("payment:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw); now = datetime.now(timezone.utc); amt = body.get("amount", 0)
    new_paid = max(0, doc.get("paid_amount", 0) - amt)
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["paid_amount"] = round(new_paid, 2); merged["outstanding_amount"] = round(doc.get("grand_total", 0) - new_paid, 2)
    merged["payment_status"] = "PAID" if merged["outstanding_amount"] <= 0.01 else "PARTIALLY_PAID" if new_paid > 0 else "PENDING"
    merged["notes"] = (merged.get("notes") or "") + f"\n[REFUND] Rs. {amt:,.2f}"; merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.post("/{bill_id}/credit-note")
async def credit_note(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    src = _dec(await _find_bill(bill_id)); now = datetime.now(timezone.utc); amt = body.get("amount", 0); bn = _generate_bill_number()
    doc = {"bill_number": bn, "client_name": src["client_name"], "quotation_id": None, "items": [], "total_quantity": 0, "total_amount": -amt, "discounts": 0, "transport_fee": 0, "taxes": 0, "grand_total": -amt, "status": "COMPLETED", "payment_status": "PAID", "paid_amount": -amt, "outstanding_amount": 0, "notes": f"Credit note for {src.get('bill_number')}: {body.get(chr(114)+chr(101)+chr(97)+chr(115)+chr(111)+chr(110), '')}", "notes_history": [], "delivery_date": None, "tags": ["credit-note"], "locked": True, "is_recurring": False, "recurring_interval": None, "recurring_end_date": None, "parent_bill_id": src["id"], "created_at": now, "updated_at": now}
    result = await shop_bills_collection.insert_one(_enc(doc)); doc["id"] = str(result.inserted_id)
    v = await bump_version("shop_bill", result.inserted_id); await enqueue_sync("shop_bill", result.inserted_id, v)
    return _dec(doc)

@router.patch("/{bill_id}/items/bulk-update")
async def bulk_items(bill_id: str, body: dict = Body(...), current_user: dict = Depends(require_capability("bill:write"))):
    raw = await _find_bill(bill_id); doc = _dec(raw)
    if doc.get("locked"): raise HTTPException(status_code=400, detail="Cannot modify a locked bill")
    items = doc.get("items", []); now = datetime.now(timezone.utc)
    for u in body.get("items", []):
        idx = u.get("index")
        if idx is not None and 0 <= idx < len(items):
            for f in ("unit_price", "quantity", "discount", "discount_type"):
                if f in u: items[idx][f] = u[f]
            items[idx]["line_total"] = _calc_item_line_total(items[idx])
    _prepare_items(items); totals = _calc_totals(items, doc.get("discounts", 0), doc.get("transport_fee", 0), doc.get("taxes", 0))
    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["items"] = items; merged.update(totals); merged["outstanding_amount"] = round(totals["grand_total"] - doc.get("paid_amount", 0), 2); merged["updated_at"] = now
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": _enc(merged)})
    v = await bump_version("shop_bill", raw["_id"]); await enqueue_sync("shop_bill", raw["_id"], v)
    return _dec(await shop_bills_collection.find_one({"_id": raw["_id"]}))

@router.get("/export/summary")
async def export_summary(current_user: dict = Depends(require_capability("bill:read"))):
    result = await shop_bills_collection.aggregate([{"$group": {"_id": None, "total_bills": {"$sum": 1}, "total_revenue": {"$sum": "$grand_total"}, "total_paid": {"$sum": "$paid_amount"}, "total_outstanding": {"$sum": "$outstanding_amount"}}}]).to_list(length=1)
    r = result[0] if result else {"total_bills": 0, "total_revenue": 0, "total_paid": 0, "total_outstanding": 0}
    return {"total_bills": r["total_bills"], "total_revenue": round(r["total_revenue"], 2), "total_paid": round(r["total_paid"], 2), "total_outstanding": round(r["total_outstanding"], 2), "exported_at": datetime.now(timezone.utc).isoformat()}

@router.get("/info")
async def api_info():
    return {"module": "Shop Bills", "version": "2.0.0", "total_features": 100}

@router.get("/recurring/due-v2")
async def due_v2(current_user: dict = Depends(require_capability("bill:read"))):
    now = datetime.now(timezone.utc)
    dm = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(weeks=1), "BIWEEKLY": timedelta(weeks=2), "MONTHLY": timedelta(days=30)}
    due = []
    async for raw in shop_bills_collection.find({"is_recurring": True}):
        doc = _dec(raw); ed = doc.get("recurring_end_date")
        if ed and isinstance(ed, str):
            try: ed = datetime.fromisoformat(ed)
            except (ValueError, TypeError): ed = None
        if ed and now > ed: continue
        interval = doc.get("recurring_interval", "MONTHLY"); ld = doc.get("updated_at") or doc.get("created_at", now)
        next_due = ld + dm.get(interval, timedelta(days=30))
        if next_due <= now: due.append({"bill_number": doc.get("bill_number"), "client_name": doc.get("client_name"), "interval": interval, "grand_total": doc.get("grand_total", 0), "next_due": next_due.isoformat()})
    return {"items": due, "total": len(due)}

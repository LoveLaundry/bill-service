"""Shop Bills router — direct billing for shops (no gate pass required).

Features:
1. Duplicate Bill
2. Bill Notes History
3. Bulk Status Update
4. Bill Templates (CRUD)
5. Quick Bill (from client's last order)
6. Bill Splitting
7. Bill Merging
8. Recurring Bills
9. Bill Expiry (auto-cancel old bills)
10. Item Discounts (per-item FIXED/PERCENT)
"""
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import shop_bills_collection, bill_templates_collection, audit_collection
from ..models import (
    ShopBillCreate, ShopBillUpdate, ShopBillPayment,
    ShopBillBulkStatus, ShopBillSplit, ShopBillMerge,
    BillTemplateCreate,
    SHOP_BILL_STATUSES, SHOP_PAYMENT_STATUSES,
)
from ..router_utils import parse_object_id, log_audit
from ..crypto_helper import get_search_token, encrypt_dict, decrypt_dict

router = APIRouter(prefix="/shop-bills", tags=["Shop Bills"])

SENSITIVE_FIELDS = ["client_name", "items", "notes"]
TEMPLATE_SENSITIVE_FIELDS = ["client_name", "items", "notes"]


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _dec_template(doc: dict) -> dict:
    try:
        decrypted = decrypt_dict(doc, TEMPLATE_SENSITIVE_FIELDS)
    except (ValueError, KeyError):
        decrypted = {k: v for k, v in doc.items() if k != "encryption_metadata" and not k.endswith("_search")}
    if "_id" in decrypted:
        decrypted["id"] = str(decrypted["_id"])
        del decrypted["_id"]
    return decrypted


def _enc_template(doc: dict) -> dict:
    to_encrypt = {k: v for k, v in doc.items() if k != "id" and k != "_id"}
    return encrypt_dict(to_encrypt, TEMPLATE_SENSITIVE_FIELDS)


def _generate_bill_number() -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"SB-{code}"


def _calc_item_line_total(item: dict) -> float:
    """Calculate line total for an item, applying per-item discount."""
    base = item.get("unit_price", 0) * item.get("quantity", 0)
    disc = item.get("discount", 0)
    disc_type = item.get("discount_type", "FIXED")
    if disc_type == "PERCENT":
        base = base * (1 - disc / 100)
    else:
        base = base - disc
    return round(max(0, base), 2)


def _calc_totals(items: list, discounts: float = 0, transport_fee: float = 0, taxes: float = 0) -> dict:
    """Calculate totals from items."""
    total_quantity = sum(item.get("quantity", 0) for item in items)
    total_amount = sum(item.get("line_total", 0) for item in items)
    grand_total = total_amount - discounts + transport_fee + taxes
    return {
        "total_quantity": total_quantity,
        "total_amount": round(total_amount, 2),
        "grand_total": round(max(0, grand_total), 2),
    }


def _prepare_items(items_data: list) -> list:
    """Recalculate line_total for all items."""
    for item in items_data:
        item["line_total"] = _calc_item_line_total(item)
    return items_data


def _build_bill_doc(payload: dict, bill_number: str, now: datetime) -> dict:
    """Build a bill document from payload dict."""
    items_data = [i.copy() for i in payload.get("items", [])]
    _prepare_items(items_data)
    totals = _calc_totals(
        items_data,
        discounts=payload.get("discounts", 0) or 0,
        transport_fee=payload.get("transport_fee", 0) or 0,
        taxes=payload.get("taxes", 0) or 0,
    )
    return {
        "bill_number": bill_number,
        "client_name": (payload.get("client_name", "") or "").strip(),
        "quotation_id": payload.get("quotation_id"),
        "items": items_data,
        "total_quantity": totals["total_quantity"],
        "total_amount": totals["total_amount"],
        "discounts": payload.get("discounts", 0) or 0.0,
        "transport_fee": payload.get("transport_fee", 0) or 0.0,
        "taxes": payload.get("taxes", 0) or 0.0,
        "grand_total": totals["grand_total"],
        "status": "PENDING",
        "payment_status": "DRAFT",
        "paid_amount": 0.0,
        "outstanding_amount": totals["grand_total"],
        "notes": payload.get("notes"),
        "notes_history": [],
        "delivery_date": payload.get("delivery_date"),
        "tags": payload.get("tags", []),
        "locked": payload.get("locked", False),
        "is_recurring": payload.get("is_recurring", False),
        "recurring_interval": payload.get("recurring_interval"),
        "recurring_end_date": payload.get("recurring_end_date"),
        "parent_bill_id": payload.get("parent_bill_id"),
        "created_at": now,
        "updated_at": now,
    }


async def _find_bill(bill_id: str) -> dict:
    """Find a bill by ObjectId or bill_number."""
    raw = await shop_bills_collection.find_one({"_id": parse_object_id(bill_id, "Bill ID")})
    if not raw:
        raw = await shop_bills_collection.find_one({"bill_number": bill_id})
    if not raw:
        raise HTTPException(status_code=404, detail="Shop bill not found")
    return raw


# ── 1. Duplicate Bill ────────────────────────────────────────────────────────

@router.post("/{bill_id}/duplicate", status_code=201)
async def duplicate_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Create a copy of an existing bill as a new draft."""
    raw = await _find_bill(bill_id)
    src = _dec(raw)
    now = datetime.now(timezone.utc)
    new_bill_number = _generate_bill_number()

    doc = {
        "bill_number": new_bill_number,
        "client_name": src["client_name"],
        "quotation_id": src.get("quotation_id"),
        "items": [i.copy() for i in src.get("items", [])],
        "total_quantity": src["total_quantity"],
        "total_amount": src["total_amount"],
        "discounts": src.get("discounts", 0),
        "transport_fee": src.get("transport_fee", 0),
        "taxes": src.get("taxes", 0),
        "grand_total": src["grand_total"],
        "status": "PENDING",
        "payment_status": "DRAFT",
        "paid_amount": 0.0,
        "outstanding_amount": src["grand_total"],
        "notes": src.get("notes"),
        "notes_history": [],
        "delivery_date": None,
        "tags": [],
        "locked": False,
        "is_recurring": False,
        "recurring_interval": None,
        "recurring_end_date": None,
        "parent_bill_id": src["id"],
        "created_at": now,
        "updated_at": now,
    }

    encrypted = _enc(doc)
    result = await shop_bills_collection.insert_one(encrypted)
    doc["id"] = str(result.inserted_id)

    await log_audit(
        current_user.get("user_name", ""),
        "duplicate",
        "shop_bill",
        str(result.inserted_id),
        details={"from_bill": bill_number(src), "new_bill": new_bill_number},
    )
    return _dec(doc)


def bill_number(doc: dict) -> str:
    return doc.get("bill_number", "")


# ── 2. Notes History ─────────────────────────────────────────────────────────

@router.get("/{bill_id}/notes-history")
async def get_notes_history(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:read")),
):
    """Get the notes change history for a bill."""
    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    return {"notes_history": doc.get("notes_history", [])}


# ── 3. Bulk Status Update ────────────────────────────────────────────────────

@router.post("/bulk-status")
async def bulk_update_status(
    payload: ShopBillBulkStatus,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Update status of multiple bills at once."""
    if payload.status not in SHOP_BILL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")

    now = datetime.now(timezone.utc)
    updated = 0
    for bid in payload.bill_ids:
        raw = await shop_bills_collection.find_one({"_id": parse_object_id(bid, "Bill ID")})
        if not raw:
            raw = await shop_bills_collection.find_one({"bill_number": bid})
        if not raw:
            continue
        doc = _dec(raw)
        if doc.get("locked"):
            continue
        merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
        merged["status"] = payload.status
        merged["updated_at"] = now
        encrypted = _enc(merged)
        await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": encrypted})
        updated += 1

    await log_audit(
        current_user.get("user_name", ""),
        "bulk_status",
        "shop_bill",
        "multiple",
        details={"count": updated, "status": payload.status},
    )
    return {"updated": updated}


# ── 4. Bill Templates CRUD ──────────────────────────────────────────────────

@router.post("/templates", status_code=201)
async def create_template(
    payload: BillTemplateCreate,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Create a bill template for quick reuse."""
    now = datetime.now(timezone.utc)
    items_data = [item.model_dump() for item in payload.items]
    _prepare_items(items_data)

    doc = {
        "name": payload.name.strip(),
        "client_name": (payload.client_name or "").strip() or None,
        "items": items_data,
        "discounts": payload.discounts or 0.0,
        "transport_fee": payload.transport_fee or 0.0,
        "taxes": payload.taxes or 0.0,
        "notes": payload.notes,
        "use_count": 0,
        "created_at": now,
        "updated_at": now,
    }

    encrypted = _enc_template(doc)
    result = await bill_templates_collection.insert_one(encrypted)
    doc["id"] = str(result.inserted_id)

    await log_audit(
        current_user.get("user_name", ""),
        "create",
        "bill_template",
        str(result.inserted_id),
        details={"name": payload.name},
    )
    return _dec_template(doc)


@router.get("/templates")
async def list_templates(
    search: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("bill:read")),
):
    """List all bill templates."""
    query: dict = {}
    if search:
        query["name"] = {"$regex": search, "$options": "i"}

    cursor = bill_templates_collection.find(query).sort("use_count", -1)
    items = []
    async for doc in cursor:
        items.append(_dec_template(doc))
    return {"items": items, "total": len(items)}


@router.get("/templates/{template_id}")
async def get_template(
    template_id: str,
    current_user: dict = Depends(require_capability("bill:read")),
):
    """Get a single template."""
    raw = await bill_templates_collection.find_one({"_id": parse_object_id(template_id, "Template ID")})
    if not raw:
        raise HTTPException(status_code=404, detail="Template not found")
    return _dec_template(raw)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Delete a template."""
    raw = await bill_templates_collection.find_one({"_id": parse_object_id(template_id, "Template ID")})
    if not raw:
        raise HTTPException(status_code=404, detail="Template not found")
    await bill_templates_collection.delete_one({"_id": raw["_id"]})
    await log_audit(
        current_user.get("user_name", ""),
        "delete",
        "bill_template",
        str(raw["_id"]),
    )
    return {"message": "Template deleted"}


# ── 5. Quick Bill ────────────────────────────────────────────────────────────

@router.post("/quick", status_code=201)
async def quick_bill(
    client_name: str = Query(...),
    template_id: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Create a bill instantly from a client's last order or a template."""
    now = datetime.now(timezone.utc)

    if template_id:
        tmpl_raw = await bill_templates_collection.find_one({"_id": parse_object_id(template_id, "Template ID")})
        if not tmpl_raw:
            raise HTTPException(status_code=404, detail="Template not found")
        tmpl = _dec_template(tmpl_raw)
        # Increment use count
        await bill_templates_collection.update_one(
            {"_id": tmpl_raw["_id"]},
            {"$inc": {"use_count": 1}},
        )
        items = [i.copy() for i in tmpl.get("items", [])]
        discounts = tmpl.get("discounts", 0)
        transport_fee = tmpl.get("transport_fee", 0)
        taxes = tmpl.get("taxes", 0)
        notes = tmpl.get("notes")
    else:
        # Find client's last bill
        last = await shop_bills_collection.find_one(
            {"client_name_search": get_search_token(client_name)},
            sort=[("created_at", -1)],
        )
        if not last:
            raise HTTPException(status_code=404, detail=f"No previous bills found for '{client_name}'")
        last_doc = _dec(last)
        items = [i.copy() for i in last_doc.get("items", [])]
        discounts = last_doc.get("discounts", 0)
        transport_fee = last_doc.get("transport_fee", 0)
        taxes = last_doc.get("taxes", 0)
        notes = None

    bill_number = _generate_bill_number()
    doc = _build_bill_doc({
        "client_name": client_name,
        "items": items,
        "discounts": discounts,
        "transport_fee": transport_fee,
        "taxes": taxes,
        "notes": notes,
    }, bill_number, now)

    encrypted = _enc(doc)
    result = await shop_bills_collection.insert_one(encrypted)
    doc["id"] = str(result.inserted_id)

    await log_audit(
        current_user.get("user_name", ""),
        "quick_create",
        "shop_bill",
        str(result.inserted_id),
        details={"bill_number": bill_number, "client": client_name, "items": len(items)},
    )
    return _dec(doc)


@router.post("/manual", status_code=201)
async def manual_bill(
    client_name: str = Query(...),
    amount: float = Query(gt=0),
    date: Optional[str] = Query(None),
    notes: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Create a simple bill for old/legacy services — just client, amount, date."""
    now = datetime.now(timezone.utc)
    bill_number = _generate_bill_number()

    delivery_date = None
    if date:
        try:
            delivery_date = datetime.fromisoformat(date.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    doc = _build_bill_doc({
        "client_name": client_name,
        "items": [{"item_name": "Service", "unit_price": amount, "quantity": 1}],
        "notes": notes,
        "delivery_date": delivery_date,
    }, bill_number, now)

    encrypted = _enc(doc)
    result = await shop_bills_collection.insert_one(encrypted)
    doc["id"] = str(result.inserted_id)

    await log_audit(
        current_user.get("user_name", ""),
        "manual_create",
        "shop_bill",
        str(result.inserted_id),
        details={"bill_number": bill_number, "client": client_name, "amount": amount},
    )

    return _dec(doc)


# ── 6. Bill Splitting ────────────────────────────────────────────────────────

@router.post("/{bill_id}/split", status_code=201)
async def split_bill(
    bill_id: str,
    payload: ShopBillSplit,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Split a bill: move specified item indices to a new bill."""
    raw = await _find_bill(bill_id)
    doc = _dec(raw)

    if doc.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot split a locked bill")
    if doc.get("payment_status") == "PAID":
        raise HTTPException(status_code=400, detail="Cannot split a fully paid bill")

    items = doc.get("items", [])
    indices = sorted(payload.item_indices, reverse=True)

    # Validate indices
    for idx in payload.item_indices:
        if idx < 0 or idx >= len(items):
            raise HTTPException(status_code=400, detail=f"Invalid item index: {idx}")

    # Extract items for new bill
    split_items = []
    for idx in indices:
        split_items.insert(0, items.pop(idx))

    if not items:
        raise HTTPException(status_code=400, detail="Cannot split: would leave original bill empty")

    now = datetime.now(timezone.utc)
    new_bill_number = _generate_bill_number()

    # Create new bill with split items
    new_doc = _build_bill_doc({
        "client_name": doc["client_name"],
        "quotation_id": doc.get("quotation_id"),
        "items": split_items,
        "discounts": 0,
        "transport_fee": 0,
        "taxes": 0,
        "notes": f"Split from {doc['bill_number']}",
        "parent_bill_id": doc["id"],
    }, new_bill_number, now)

    encrypted_new = _enc(new_doc)
    result = await shop_bills_collection.insert_one(encrypted_new)
    new_doc["id"] = str(result.inserted_id)

    # Update original bill
    orig_totals = _calc_totals(items, doc.get("discounts", 0), doc.get("transport_fee", 0), doc.get("taxes", 0))
    orig_merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    orig_merged["items"] = items
    orig_merged["total_quantity"] = orig_totals["total_quantity"]
    orig_merged["total_amount"] = orig_totals["total_amount"]
    orig_merged["grand_total"] = orig_totals["grand_total"]
    orig_merged["outstanding_amount"] = round(orig_totals["grand_total"] - doc.get("paid_amount", 0), 2)
    orig_merged["updated_at"] = now
    encrypted_orig = _enc(orig_merged)
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": encrypted_orig})

    await log_audit(
        current_user.get("user_name", ""),
        "split",
        "shop_bill",
        str(raw["_id"]),
        details={"from": doc["bill_number"], "new_bill": new_bill_number, "items_moved": len(split_items)},
    )
    return _dec(new_doc)


# ── 7. Bill Merging ──────────────────────────────────────────────────────────

@router.post("/merge", status_code=201)
async def merge_bills(
    payload: ShopBillMerge,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Merge multiple bills into one. First bill in list becomes the base."""
    if len(payload.bill_ids) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 bills to merge")

    base_raw = await _find_bill(payload.bill_ids[0])
    base = _dec(base_raw)

    if base.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot merge into a locked bill")

    now = datetime.now(timezone.utc)
    all_items = list(base.get("items", []))
    merged_from = [base["bill_number"]]

    for bid in payload.bill_ids[1:]:
        raw = await _find_bill(bid)
        doc = _dec(raw)
        if doc.get("locked"):
            raise HTTPException(status_code=400, detail=f"Cannot merge locked bill {doc['bill_number']}")
        all_items.extend(doc.get("items", []))
        merged_from.append(doc["bill_number"])
        # Delete the merged bill
        await shop_bills_collection.delete_one({"_id": raw["_id"]})

    _prepare_items(all_items)
    totals = _calc_totals(all_items, base.get("discounts", 0), base.get("transport_fee", 0), base.get("taxes", 0))

    merged = {k: v for k, v in base.items() if k not in ("id", "_id")}
    merged["items"] = all_items
    merged["total_quantity"] = totals["total_quantity"]
    merged["total_amount"] = totals["total_amount"]
    merged["grand_total"] = totals["grand_total"]
    merged["outstanding_amount"] = round(totals["grand_total"] - base.get("paid_amount", 0), 2)
    merged["updated_at"] = now
    merged["notes"] = (merged.get("notes") or "") + f"\nMerged from: {', '.join(merged_from[1:])}"

    encrypted = _enc(merged)
    await shop_bills_collection.update_one({"_id": base_raw["_id"]}, {"$set": encrypted})

    await log_audit(
        current_user.get("user_name", ""),
        "merge",
        "shop_bill",
        str(base_raw["_id"]),
        details={"merged_bills": merged_from, "total_items": len(all_items)},
    )
    updated = await shop_bills_collection.find_one({"_id": base_raw["_id"]})
    return _dec(updated)


# ── 8. Recurring Bills ───────────────────────────────────────────────────────

@router.post("/{bill_id}/make-recurring")
async def make_recurring(
    bill_id: str,
    interval: str = Query(..., description="DAILY, WEEKLY, BIWEEKLY, MONTHLY"),
    end_date: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Set a bill as recurring to auto-generate copies."""
    valid_intervals = ["DAILY", "WEEKLY", "BIWEEKLY", "MONTHLY"]
    if interval not in valid_intervals:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Must be one of: {valid_intervals}")

    raw = await _find_bill(bill_id)
    doc = _dec(raw)
    now = datetime.now(timezone.utc)

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged["is_recurring"] = True
    merged["recurring_interval"] = interval
    merged["recurring_end_date"] = datetime.fromisoformat(end_date) if end_date else None
    merged["updated_at"] = now

    encrypted = _enc(merged)
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": encrypted})

    await log_audit(
        current_user.get("user_name", ""),
        "make_recurring",
        "shop_bill",
        str(raw["_id"]),
        details={"bill_number": doc["bill_number"], "interval": interval},
    )
    updated = await shop_bills_collection.find_one({"_id": raw["_id"]})
    return _dec(updated)


@router.get("/recurring/due")
async def get_recurring_due(
    current_user: dict = Depends(require_capability("bill:read")),
):
    """Get recurring bills that are due for auto-generation."""
    now = datetime.now(timezone.utc)
    cursor = shop_bills_collection.find({"is_recurring": True})
    due = []
    async for raw in cursor:
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
        delta_map = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(weeks=1), "BIWEEKLY": timedelta(weeks=2), "MONTHLY": timedelta(days=30)}
        due.append({
            "bill_number": doc["bill_number"],
            "client_name": doc["client_name"],
            "interval": interval,
            "grand_total": doc["grand_total"],
            "next_due": (doc.get("updated_at") or doc.get("created_at", now)),
        })
    return {"items": due, "total": len(due)}


# ── 9. Bill Expiry ───────────────────────────────────────────────────────────

@router.get("/expiring")
async def get_expiring_bills(
    days: int = Query(7, ge=1, le=90),
    current_user: dict = Depends(require_capability("bill:read")),
):
    """Get bills approaching delivery date expiry."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)

    cursor = shop_bills_collection.find({
        "delivery_date": {"$lte": cutoff, "$gte": now},
        "status": {"$nin": ["COMPLETED", "CANCELLED"]},
    }).sort("delivery_date", 1)

    items = []
    async for doc in cursor:
        items.append(_dec(doc))
    return {"items": items, "total": len(items)}


@router.post("/expire-old")
async def expire_old_bills(
    max_days: int = Query(30, ge=1),
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Auto-cancel bills older than max_days with DRAFT payment status."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_days)

    cursor = shop_bills_collection.find({
        "created_at": {"$lt": cutoff},
        "payment_status": "DRAFT",
        "status": {"$nin": ["COMPLETED", "CANCELLED"]},
    })

    expired = 0
    async for raw in cursor:
        doc = _dec(raw)
        merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
        merged["status"] = "CANCELLED"
        merged["payment_status"] = "CANCELLED"
        merged["updated_at"] = now
        encrypted = _enc(merged)
        await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": encrypted})
        expired += 1

    await log_audit(
        current_user.get("user_name", ""),
        "expire",
        "shop_bill",
        "batch",
        details={"expired_count": expired, "max_days": max_days},
    )
    return {"expired": expired}


# ── Standard CRUD ────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_shop_bill(
    payload: ShopBillCreate,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Create a new shop bill."""
    now = datetime.now(timezone.utc)
    bill_number = payload.bill_number.strip() if payload.bill_number else _generate_bill_number()

    existing = await shop_bills_collection.find_one({"bill_number": bill_number})
    if existing:
        raise HTTPException(status_code=409, detail=f"Bill number {bill_number} already exists")

    doc = _build_bill_doc(payload.model_dump(), bill_number, now)

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
    sort_by: str = Query("created_at", regex="^(created_at|grand_total|client_name|delivery_date)$"),
    sort_order: str = Query("desc", regex="^(asc|desc)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_capability("bill:read")),
):
    """List shop bills with optional filters and sorting."""
    query: dict = {}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)
    if status:
        query["status"] = status
    if payment_status:
        query["payment_status"] = payment_status
    if search:
        query["$or"] = [
            {"bill_number": {"$regex": search, "$options": "i"}},
            {"client_name_search": {"$regex": search, "$options": "i"}},
        ]

    total = await shop_bills_collection.count_documents(query)
    sort_dir = -1 if sort_order == "desc" else 1
    cursor = shop_bills_collection.find(query).sort(sort_by, sort_dir).skip(skip).limit(limit)
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
    raw = await _find_bill(bill_id)
    return _dec(raw)


@router.patch("/{bill_id}")
async def update_shop_bill(
    bill_id: str,
    payload: ShopBillUpdate,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Update a shop bill."""
    raw = await _find_bill(bill_id)
    doc = _dec(raw)

    if doc.get("locked") and payload.status is None:
        raise HTTPException(status_code=400, detail="This bill is locked. Unlock it first to make changes.")

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
        # Track notes history
        history = doc.get("notes_history", [])
        if payload.notes != doc.get("notes"):
            history.append({
                "old_notes": doc.get("notes", ""),
                "new_notes": payload.notes,
                "changed_by": current_user.get("user_name", ""),
                "changed_at": now.isoformat(),
            })
        update_fields["notes"] = payload.notes
        update_fields["notes_history"] = history

    if payload.delivery_date is not None:
        update_fields["delivery_date"] = payload.delivery_date

    if payload.items is not None:
        items_data = [item.model_dump() for item in payload.items]
        _prepare_items(items_data)
        update_fields["items"] = items_data
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

    if payload.tags is not None:
        update_fields["tags"] = payload.tags

    if payload.locked is not None:
        update_fields["locked"] = payload.locked

    merged = {k: v for k, v in doc.items() if k not in ("id", "_id")}
    merged.update(update_fields)
    encrypted = _enc(merged)
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": encrypted})

    await log_audit(
        current_user.get("user_name", ""),
        "update",
        "shop_bill",
        str(raw["_id"]),
        details={"bill_number": doc.get("bill_number"), "changes": list(update_fields.keys())},
    )

    updated = await shop_bills_collection.find_one({"_id": raw["_id"]})
    return _dec(updated)


@router.post("/{bill_id}/payment")
async def record_payment(
    bill_id: str,
    payload: ShopBillPayment,
    current_user: dict = Depends(require_capability("payment:write")),
):
    """Record a payment against a shop bill."""
    raw = await _find_bill(bill_id)
    doc = _dec(raw)

    if doc.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot record payment on a locked bill")
    if doc.get("status") == "CANCELLED":
        raise HTTPException(status_code=400, detail="Cannot record payment on a cancelled bill")

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
    await shop_bills_collection.update_one({"_id": raw["_id"]}, {"$set": encrypted})

    await log_audit(
        current_user.get("user_name", ""),
        "payment",
        "shop_bill",
        str(raw["_id"]),
        details={"bill_number": doc.get("bill_number"), "amount": payload.amount, "method": payload.payment_method},
    )

    updated = await shop_bills_collection.find_one({"_id": raw["_id"]})
    return _dec(updated)


@router.delete("/{bill_id}")
async def delete_shop_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:write")),
):
    """Delete a shop bill."""
    raw = await _find_bill(bill_id)
    doc = _dec(raw)

    if doc.get("locked"):
        raise HTTPException(status_code=400, detail="Cannot delete a locked bill")

    await shop_bills_collection.delete_one({"_id": raw["_id"]})

    await log_audit(
        current_user.get("user_name", ""),
        "delete",
        "shop_bill",
        str(raw["_id"]),
        details={"bill_number": doc.get("bill_number")},
    )
    return {"message": "Shop bill deleted"}

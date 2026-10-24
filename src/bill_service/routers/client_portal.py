"""Hotel client portal — read-only views for an external hotel account.

Isolation model
---------------
A hotel session token carries ``token_kind == "client"`` and a ``client_scope``
pair, and **no** ``laundry_id`` / ``laundries`` claim. Every staff endpoint in
this service resolves its tenant through ``resolve_laundry_id``, which fails
closed without those claims — so a hotel token is refused everywhere except
this router. Here the scope is resolved explicitly and every query is filtered
by *both* the laundry and the hotel name:

    {"client_name_search": get_search_token(name)} + {"laundry_id": ...}

``client_name`` is stored encrypted, so the filter must use the deterministic
search token (exactly like the staff endpoints do) and results must be passed
through ``decrypt_dict`` before they leave the service.

Read-only by construction: this router defines no write verb. A hotel cannot
create a gate pass, edit a bill, or touch a payment; those stay staff actions.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Security
from bson import ObjectId

from ..app_time import month_bounds
from ..auth_helper import get_current_user
from ..crypto_helper import decrypt_dict, get_search_token
from ..database.main_db import (
    bills_collection,
    deliveries_collection,
    gatepasses_collection,
    shop_bills_collection,
)
from ..tenancy import scoped_query

router = APIRouter(prefix="/client-portal", tags=["client-portal"])

GP_SENSITIVE = ["client_name", "items", "notes"]
DELIVERY_SENSITIVE = ["client_name", "items", "notes"]
BILL_SENSITIVE = ["client_name", "quotation_title", "notes", "items"]

# A pass is still "with the laundry" until every piece has come back.
OPEN_GATEPASS_STATUSES = (
    "RECEIVED",
    "PROCESSING",
    "READY_FOR_DELIVERY",
    "PARTIALLY_DELIVERED",
)

# Statuses that can still carry money the hotel owes.
OUTSTANDING_STATUSES = ("PENDING", "ISSUED", "PARTIALLY_PAID")

# Hard ceiling on rows scanned for a money total, so one hotel with years of
# history cannot turn a dashboard request into an unbounded read.
_SUM_SCAN_LIMIT = 5000


def resolve_client_scope(current_user: dict = Security(get_current_user)) -> Dict[str, str]:
    """Return the (laundry, hotel) pair this request may read.

    Fails closed: a staff token, a support token, or a malformed scope is a 403
    rather than a silently empty result.
    """
    if not isinstance(current_user, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")

    if current_user.get("token_kind") != "client":
        raise HTTPException(status_code=403, detail="This endpoint requires a hotel account")

    scope = current_user.get("client_scope")
    if not isinstance(scope, dict):
        raise HTTPException(status_code=403, detail="Hotel session has no account scope")

    laundry_id = str(scope.get("laundry_id") or "").strip().lower()
    client_name = str(scope.get("client_name") or "").strip()
    if not laundry_id or not client_name:
        raise HTTPException(status_code=403, detail="Hotel session has an incomplete scope")

    client_type = str(scope.get("client_type") or "HOTEL").strip().upper()
    if client_type not in ("HOTEL", "SHOP"):
        raise HTTPException(status_code=403, detail="Client session has an unknown account type")

    return {
        "laundry_id": laundry_id,
        "client_name": client_name,
        "client_type": client_type,
    }


def resolve_hotel_scope(current_user: dict = Security(get_current_user)) -> Dict[str, str]:
    return _side_scope(current_user, "HOTEL")


def resolve_shop_scope(current_user: dict = Security(get_current_user)) -> Dict[str, str]:
    return _side_scope(current_user, "SHOP")


def _side_scope(current_user: dict, side: str) -> Dict[str, str]:
    scope = resolve_client_scope(current_user)
    require_side(scope, side)
    return scope


def require_side(scope: Dict[str, str], side: str) -> None:
    """Refuse a portal call made by the other side of the business.

    A shop login and a hotel login can belong to the same person and even the
    same laundry; the side on the token is what keeps their data apart.
    """
    if scope.get("client_type") != side:
        raise HTTPException(
            status_code=403,
            detail=f"This endpoint is for {side.lower()} accounts only",
        )


def _client_query(scope: Dict[str, str]) -> dict:
    """Tenant + hotel filter, built in exactly one place."""
    return scoped_query(
        {"client_name_search": get_search_token(scope["client_name"])},
        scope["laundry_id"],
    )


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _stamp(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _object_id(value: str) -> ObjectId:
    """Parse a document id, or 404 — an unparseable id is not a 500."""
    try:
        return ObjectId(str(value))
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/summary")
async def portal_summary(
    scope: Dict[str, str] = Security(resolve_hotel_scope),
) -> dict:
    """Headline numbers for the hotel dashboard.

    Totals are computed from stored documents only — this service owns the
    arithmetic, the app never re-adds money.
    """
    query = _client_query(scope)

    open_passes = await gatepasses_collection.count_documents(
        {**query, "status": {"$in": list(OPEN_GATEPASS_STATUSES)}}
    )

    today = date.today()
    start, end = month_bounds(today.year, today.month)

    deliveries_this_month = await deliveries_collection.count_documents(
        {
            **query,
            "status": {"$ne": "CANCELLED"},
            "delivery_date": {"$gte": start, "$lt": end},
        }
    )

    outstanding = 0.0
    invoiced_this_month = 0.0
    scanned = 0

    cursor = bills_collection.find(
        query,
        {
            "grand_total": 1,
            "paid_amount": 1,
            "payment_status": 1,
            "created_at": 1,
        },
    ).limit(_SUM_SCAN_LIMIT)

    async for doc in cursor:
        scanned += 1
        status = str(doc.get("payment_status") or "").upper()
        if status == "CANCELLED":
            continue
        grand = _money(doc.get("grand_total"))
        if status in OUTSTANDING_STATUSES:
            outstanding += grand - _money(doc.get("paid_amount"))
        created = doc.get("created_at")
        if isinstance(created, date):
            created_day = created if type(created) is date else created.date()
            if start.date() <= created_day < end.date():
                invoiced_this_month += grand

    return {
        "hotel": scope["client_name"],
        "laundry_id": scope["laundry_id"],
        "open_gate_passes": open_passes,
        "deliveries_this_month": deliveries_this_month,
        "outstanding_amount": _money(outstanding),
        "invoiced_this_month": _money(invoiced_this_month),
        "currency": "LKR",
        # Honest about truncation instead of presenting a wrong total as exact.
        "totals_scanned": scanned,
        "totals_truncated": scanned >= _SUM_SCAN_LIMIT,
    }


@router.get("/gate-passes")
async def portal_gate_passes(
    status: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    scope: Dict[str, str] = Security(resolve_hotel_scope),
) -> dict:
    query = _client_query(scope)
    if status:
        query = {**query, "status": status.strip().upper()}

    total = await gatepasses_collection.count_documents(query)
    cursor = (
        gatepasses_collection.find(query)
        .sort("receiving_date", -1)
        .skip(skip)
        .limit(limit)
    )

    items: List[dict] = []
    async for doc in cursor:
        decrypted = decrypt_dict(doc, GP_SENSITIVE)
        items.append(
            {
                "id": str(decrypted.get("_id") or ""),
                "gate_pass_number": decrypted.get("gate_pass_number"),
                "client_name": decrypted.get("client_name"),
                "receiving_date": _stamp(decrypted.get("receiving_date")),
                "received_by": decrypted.get("received_by"),
                "status": decrypted.get("status"),
                "items": decrypted.get("items") or [],
                "notes": decrypted.get("notes"),
            }
        )

    return {"total": total, "skip": skip, "limit": limit, "items": items}


@router.get("/deliveries")
async def portal_deliveries(
    status: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    scope: Dict[str, str] = Security(resolve_hotel_scope),
) -> dict:
    query = _client_query(scope)
    if status:
        query = {**query, "status": status.strip().upper()}

    total = await deliveries_collection.count_documents(query)
    cursor = (
        deliveries_collection.find(query)
        .sort("delivery_date", -1)
        .skip(skip)
        .limit(limit)
    )

    items: List[dict] = []
    async for doc in cursor:
        decrypted = decrypt_dict(doc, DELIVERY_SENSITIVE)
        items.append(
            {
                "id": str(decrypted.get("_id") or ""),
                "gate_pass_id": decrypted.get("gate_pass_id"),
                "client_name": decrypted.get("client_name"),
                "delivery_date": _stamp(decrypted.get("delivery_date")),
                "delivered_by": decrypted.get("delivered_by"),
                "received_by": decrypted.get("received_by"),
                "status": decrypted.get("status"),
                "items": decrypted.get("items") or [],
                "corrections": decrypted.get("corrections") or [],
            }
        )

    return {"total": total, "skip": skip, "limit": limit, "items": items}


@router.get("/bills")
async def portal_bills(
    payment_status: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    scope: Dict[str, str] = Security(resolve_hotel_scope),
) -> dict:
    query = _client_query(scope)
    if payment_status:
        query = {**query, "payment_status": payment_status.strip().upper()}

    total = await bills_collection.count_documents(query)
    cursor = bills_collection.find(query).sort("created_at", -1).skip(skip).limit(limit)

    items: List[dict] = []
    async for doc in cursor:
        decrypted = decrypt_dict(doc, BILL_SENSITIVE)
        grand = _money(decrypted.get("grand_total"))
        paid = _money(decrypted.get("paid_amount"))
        items.append(
            {
                "id": str(decrypted.get("_id") or ""),
                "manual_bill_number": decrypted.get("manual_bill_number"),
                "quotation_title": decrypted.get("quotation_title"),
                "created_at": _stamp(decrypted.get("created_at")),
                "total_quantity": decrypted.get("total_quantity"),
                "grand_total": grand,
                "paid_amount": paid,
                "balance": _money(grand - paid),
                "payment_status": decrypted.get("payment_status"),
            }
        )

    return {"total": total, "skip": skip, "limit": limit, "items": items}


@router.get("/bills/{bill_id}")
async def portal_bill_detail(
    bill_id: str,
    scope: Dict[str, str] = Security(resolve_hotel_scope),
) -> dict:
    """One invoice, but only if it belongs to this hotel.

    The client filter is part of the lookup rather than a later check, so a
    guessed id from another hotel is indistinguishable from a missing one.
    """
    try:
        oid = ObjectId(bill_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Invoice not found")

    doc = await bills_collection.find_one({**_client_query(scope), "_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Invoice not found")

    decrypted = decrypt_dict(doc, BILL_SENSITIVE)
    grand = _money(decrypted.get("grand_total"))
    paid = _money(decrypted.get("paid_amount"))

    return {
        "id": str(decrypted.get("_id") or ""),
        "manual_bill_number": decrypted.get("manual_bill_number"),
        "quotation_title": decrypted.get("quotation_title"),
        "created_at": _stamp(decrypted.get("created_at")),
        "items": decrypted.get("items") or [],
        "total_quantity": decrypted.get("total_quantity"),
        "total_amount": _money(decrypted.get("total_amount")),
        "discounts": _money(decrypted.get("discounts")),
        "transport_fee": _money(decrypted.get("transport_fee")),
        "taxes": _money(decrypted.get("taxes")),
        "additional_charges": _money(decrypted.get("additional_charges")),
        "grand_total": grand,
        "paid_amount": paid,
        "balance": _money(grand - paid),
        "payment_status": decrypted.get("payment_status"),
        "notes": decrypted.get("notes"),
    }


# ── Shop portal ────────────────────────────────────────────────────────────────
# Same rules as the hotel portal, but the filter is the shop account's own
# client_name and the side check is SHOP. A hotel token is refused here, and a
# shop token is refused by every hotel endpoint above.

SHOP_BILL_PROJECTION = {
    "_id": 1,
    "bill_number": 1,
    "client_name": 1,
    "grand_total": 1,
    "paid_amount": 1,
    "outstanding_amount": 1,
    "payment_status": 1,
    "status": 1,
    "total_quantity": 1,
    "delivery_date": 1,
    "created_at": 1,
}


@router.get("/shop/summary")
async def shop_summary(scope: Dict[str, str] = Security(resolve_shop_scope)) -> dict:
    """Headline numbers for one shop account."""
    query = _client_query(scope)

    bills_open = 0
    outstanding = 0.0
    invoiced_month = 0.0
    scanned = 0
    async for doc in shop_bills_collection.find(query, SHOP_BILL_PROJECTION).limit(5000):
        scanned += 1
        status = str(doc.get("payment_status") or "").upper()
        if status not in ("PAID", "CANCELLED", "DRAFT"):
            bills_open += 1
        if status in OUTSTANDING_STATUSES:
            outstanding += _money(doc.get("outstanding_amount"))
        created = doc.get("created_at")
        if isinstance(created, date):
            start, end = month_bounds(created.year, created.month)
            if start <= created < end and status != "CANCELLED":
                invoiced_month += _money(doc.get("grand_total"))

    return {
        "shop": scope["client_name"],
        "laundry_id": scope["laundry_id"],
        "open_bills": bills_open,
        "outstanding_amount": round(outstanding, 2),
        "invoiced_this_month": round(invoiced_month, 2),
        "currency": "LKR",
        "scanned": scanned,
    }


@router.get("/shop/bills")
async def shop_bills(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    scope: Dict[str, str] = Security(resolve_shop_scope),
) -> dict:
    """This shop's bills, newest first."""
    query = _client_query(scope)
    if status:
        query = {**query, "payment_status": status.strip().upper()}

    total = await shop_bills_collection.count_documents(query)
    cursor = (
        shop_bills_collection.find(query, SHOP_BILL_PROJECTION)
        .sort("created_at", -1)
        .skip(offset)
        .limit(limit)
    )

    items = []
    async for doc in cursor:
        items.append(
            {
                "id": str(doc.get("_id")),
                "bill_number": doc.get("bill_number"),
                "client_name": scope["client_name"],
                "created_at": _stamp(doc.get("created_at")),
                "delivery_date": _stamp(doc.get("delivery_date")),
                "grand_total": _money(doc.get("grand_total")),
                "paid_amount": _money(doc.get("paid_amount")),
                "balance": _money(doc.get("outstanding_amount")),
                "payment_status": doc.get("payment_status"),
                "status": doc.get("status"),
                "total_quantity": doc.get("total_quantity"),
            }
        )

    return {"total": total, "count": len(items), "items": items}


@router.get("/shop/bills/{bill_id}")
async def shop_bill_detail(
    bill_id: str,
    scope: Dict[str, str] = Security(resolve_shop_scope),
) -> dict:
    """One bill, only if it belongs to the signed-in shop."""
    oid = _object_id(bill_id)
    doc = await shop_bills_collection.find_one({**_client_query(scope), "_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Bill not found")

    clean = decrypt_dict(doc, BILL_SENSITIVE)
    return {
        "id": str(clean.get("_id")),
        "bill_number": clean.get("bill_number"),
        "client_name": scope["client_name"],
        "created_at": _stamp(clean.get("created_at")),
        "delivery_date": _stamp(clean.get("delivery_date")),
        "items": clean.get("items") or [],
        "total_quantity": clean.get("total_quantity"),
        "grand_total": _money(clean.get("grand_total")),
        "paid_amount": _money(clean.get("paid_amount")),
        "balance": _money(clean.get("outstanding_amount")),
        "payment_status": clean.get("payment_status"),
        "status": clean.get("status"),
        "notes": clean.get("notes"),
    }
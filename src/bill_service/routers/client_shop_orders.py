"""Shop orders: a shop submits laundry, the laundry confirms receipt.

The object created is an ordinary gate pass in ``DRAFT`` — deliberately not a
new collection and not a new status. That gives shop orders the same journal,
sync, audit and verification chain as a gate pass raised at the counter, and it
means the laundry confirms receipt through the transition the workflow already
guards: ``DRAFT -> RECEIVED`` (see ``gatepasses.update_gate_pass_status``).

``received_qty`` starts at zero and is only filled when the laundry counts the
linen in. The shop's figure is ``client_qty``; the difference between the two
is the discrepancy the existing flows already know how to report.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..crypto_helper import decrypt_dict
from ..database.main_db import gatepasses_collection, payments_collection, shop_bills_collection
from ..services.gate_pass_records import create_gate_pass_record
from ..models import GatePassCreate, GatePassItem
from ..tenancy import scoped_query
from .client_portal import BILL_SENSITIVE, GP_SENSITIVE, resolve_client_scope

router = APIRouter(prefix="/client-portal", tags=["client-portal"])


def resolve_shop_scope(session: dict = Depends(resolve_client_scope)) -> Dict[str, str]:
    """Shop sessions only — a hotel must not raise a laundry order."""
    if session.get("client_scope", {}).get("client_type") != "SHOP":
        raise HTTPException(status_code=403, detail="This endpoint is for shop accounts")
    return session


class ShopOrderLine(BaseModel):
    item_name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(gt=0)
    unit: Optional[str] = Field(default=None, max_length=24)
    specification: Optional[str] = Field(default=None, max_length=120)


class ShopOrderCreate(BaseModel):
    items: List[ShopOrderLine] = Field(min_length=1)
    notes: Optional[str] = Field(default=None, max_length=500)
    requested_by: Optional[str] = Field(default=None, max_length=80)


def _number() -> str:
    """A request number the laundry can quote before it assigns its own."""
    return "REQ-%s-%s" % (
        datetime.now(timezone.utc).strftime("%y%m%d"),
        uuid4().hex[:5].upper(),
    )


def _order_view(doc: Dict[str, Any]) -> Dict[str, Any]:
    # Tolerate an already-decrypted document: create_gate_pass_record returns a
    # serialized view, while list/detail endpoints hand over the stored row.
    try:
        clean = decrypt_dict(doc, GP_SENSITIVE)
    except Exception:
        clean = doc
    items = clean.get("items") or []
    pieces = 0.0
    for item in items:
        if isinstance(item, dict):
            pieces += float(item.get("client_qty") or 0)

    received = doc.get("receiving_date")
    return {
        "id": str(doc.get("_id")),
        "gate_pass_number": clean.get("gate_pass_number"),
        "status": clean.get("status"),
        "receiving_date": received.isoformat() if hasattr(received, "isoformat") else received,
        "notes": clean.get("notes"),
        "pieces": round(pieces, 2),
        "line_count": len(items),
        "items": [
            {
                "item_name": item.get("item_name"),
                "quantity": item.get("client_qty"),
                "received_qty": item.get("received_qty"),
                "unit": item.get("unit"),
            }
            for item in items
            if isinstance(item, dict)
        ],
    }


@router.post("/shop/orders", status_code=201)
async def create_shop_order(
    payload: ShopOrderCreate,
    session: dict = Depends(resolve_shop_scope),
):
    """Submit laundry to the laundry. Lands as a DRAFT gate pass awaiting the
    laundry's receipt confirmation."""
    scope = session["client_scope"]
    laundry_id = scope["laundry_id"]

    items = [
        GatePassItem(
            item_name=line.item_name.strip(),
            specification=(line.specification or None),
            unit=(line.unit or None),
            client_qty=float(line.quantity),
            # The shop is stating what it sent; the laundry counts what arrived.
            received_qty=0,
        )
        for line in payload.items
    ]

    order = GatePassCreate(
        gate_pass_number=_number(),
        client_name=scope["client_name"],
        receiving_date=datetime.now(timezone.utc),
        received_by=(payload.requested_by or session.get("user_name") or "Shop")[:80],
        items=items,
        notes=(payload.notes or None),
        status="DRAFT",
        origin={"kind": "shop_order"},
    )

    actor = {
        "auth_id": session.get("auth_id"),
        "user_name": session.get("user_name"),
        "role": session.get("role") or "SHOP_ADMIN",
        "laundry_id": laundry_id,
        "client_scope": scope,
    }

    try:
        created = await create_gate_pass_record(
            order,
            actor,
            status="DRAFT",
            origin={"kind": "shop_order"},
            laundry_id=laundry_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # create_gate_pass_record returns a serialized (already decrypted) view.
    # The row it wrote is encrypted at rest, so re-read it and decrypt through
    # the same path every other portal endpoint uses. Falling back to the
    # serialized object keeps this working if the id is absent.
    raw = None
    created_id = created.get("id") if isinstance(created, dict) else None
    if created_id:
        try:
            raw = await gatepasses_collection.find_one({"_id": ObjectId(str(created_id))})
        except Exception:
            raw = None

    return _order_view(raw or created)


@router.get("/shop/orders")
async def list_shop_orders(
    session: dict = Depends(resolve_shop_scope),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
):
    """This shop's orders at the currently selected laundry, newest first."""
    from ..crypto_helper import get_search_token

    scope = session["client_scope"]
    query = scoped_query(
        {"client_name_search": get_search_token(scope["client_name"])},
        scope["laundry_id"],
    )

    total = await gatepasses_collection.count_documents(query)
    cursor = (
        gatepasses_collection.find(query)
        .sort("receiving_date", -1)
        .skip(skip)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    return {"total": total, "items": [_order_view(doc) for doc in docs]}


@router.get("/shop/orders/{order_id}")
async def get_shop_order(order_id: str, session: dict = Depends(resolve_shop_scope)):
    from ..crypto_helper import get_search_token

    scope = session["client_scope"]
    try:
        oid = ObjectId(order_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Order not found")

    query = scoped_query(
        {"_id": oid, "client_name_search": get_search_token(scope["client_name"])},
        scope["laundry_id"],
    )
    doc = await gatepasses_collection.find_one(query)
    if not doc:
        # 404 rather than 403: another shop's order must not be distinguishable
        # from one that does not exist.
        raise HTTPException(status_code=404, detail="Order not found")
    return _order_view(doc)


@router.get("/shop/bills/{bill_id}")
async def get_shop_bill(bill_id: str, session: dict = Depends(resolve_shop_scope)):
    """One bill in full, plus its payments — the shop's own bill only."""
    from ..crypto_helper import get_search_token

    scope = session["client_scope"]
    try:
        oid = ObjectId(bill_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Bill not found")

    query = scoped_query(
        {"_id": oid, "client_name_search": get_search_token(scope["client_name"])},
        scope["laundry_id"],
    )
    doc = await shop_bills_collection.find_one(query)
    if not doc:
        raise HTTPException(status_code=404, detail="Bill not found")

    clean = decrypt_dict(doc, BILL_SENSITIVE)
    created = clean.get("created_at")
    paid = float(clean.get("paid_amount") or 0)
    total = float(clean.get("grand_total") or 0)

    payment_docs = await payments_collection.find(
        scoped_query({"shop_bill_id": str(doc.get("_id"))}, scope["laundry_id"])
    ).to_list(length=200)

    return {
        "id": str(doc.get("_id")),
        "bill_number": clean.get("bill_number"),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "grand_total": round(total, 2),
        "paid_amount": round(paid, 2),
        "balance": round(total - paid, 2),
        "payment_status": clean.get("payment_status"),
        "status": clean.get("status"),
        "items": clean.get("items") or [],
        "notes": clean.get("notes"),
        "payments": [
            {
                "id": str(p.get("_id")),
                "amount": float(p.get("amount") or 0),
                "method": p.get("method"),
                "paid_at": (
                    p.get("paid_at").isoformat() if hasattr(p.get("paid_at"), "isoformat")
                    else p.get("paid_at")
                ),
            }
            for p in payment_docs
        ],
    }
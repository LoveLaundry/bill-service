"""Monthly Operations — Receiving / Deliveries / Rewash grids.

One month matrix per (client_name, kind, year, month) lives in
``monthly_entries``. A day starts as DRAFT while quantities are entered, and
CONFIRMING a day creates REAL records (a DRAFT gate pass, DRAFT delivery(s),
or rewash records) that a user later completes and activates from the normal
pages — this module is NOT a second system of record, it is the input surface
for normal records.

Invariants enforced here:
  * no negative quantities (quantities PUT + confirm both reject)
  * no empty-day confirmation (all-zero totals are rejected)
  * day confirmation is idempotent (X-Idempotency-Key on POST confirm)
  * one day confirm happens once (status guard: CONFIRMED days are immutable)
  * month lengths + leap years honoured (calendar.monthrange)
  * delivery day sources: same_day GP, FIFO across pending, or manual per-GP
  * prices always resolve from the quotation (never hardcoded)
"""
import calendar
import math
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import (
    audit_collection,
    deliveries_collection,
    gatepasses_collection,
    monthly_entries_collection,
    rewashes_collection,
)
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services import idempotency
from ..services import balance_engine as be
from ..services.gate_pass_records import create_gate_pass_record, next_receiving_number
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_MONTHLY_DAY_CONFIRMED,
    EVENT_REWASH_RECORDED,
)
from ..models import (
    DeliveryItem,
    GatePassCreate,
    GatePassItem,
    MonthlyDayConfirm,
    MonthlyMatrixResponse,
    MonthlyQuantitiesUpdate,
    MONTHLY_KINDS,
    RewashItem,
)

router = APIRouter(prefix="/monthly", tags=["monthly"])

MONTH_SENSITIVE_FIELDS = ["client_name", "notes"]
REWASH_SENSITIVE_FIELDS = ["client_name", "notes"]
DELIVERY_SENSITIVE_FIELDS = ["client_name", "items", "notes"]
GATEPASS_SENSITIVE_FIELDS = ["client_name", "items", "notes"]
QUOTATION_SENSITIVE_FIELDS = ["client_name", "quotation_title", "line_items"]


# --------------------------------------------------------------------------
# Serialization / document helpers
# --------------------------------------------------------------------------
def _serialize_month(doc: dict) -> dict:
    decrypted = decrypt_dict(doc, MONTH_SENSITIVE_FIELDS)
    decrypted["id"] = str(decrypted["_id"])
    del decrypted["_id"]
    decrypted.pop("client_name_search", None)
    return decrypted


def _parse_month_numbers(year: int, month: int) -> int:
    try:
        return calendar.monthrange(year, month)[1]
    except (ValueError, OverflowError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid year/month {year}-{month}.",
        )


async def _find_gate_pass(gp_id: str) -> Optional[dict]:
    try:
        oid = ObjectId(gp_id)
    except InvalidId:
        return None
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        return None
    return decrypt_dict(doc, GATEPASS_SENSITIVE_FIELDS)


async def _find_quotation(quotation_id: Optional[str]) -> Optional[dict]:
    """Read a quotation from the MAIN cluster (mirrors bills.py)."""
    from ..database.connection_manager import get_client

    if not quotation_id:
        return None
    try:
        q_oid = ObjectId(quotation_id)
    except Exception:
        return None
    motor_client = get_client("MAIN")
    for db_name in ("quotations_db", "quotations", "laundry_db"):
        try:
            coll = motor_client[db_name]["quotations"]
            qu = await coll.find_one({"_id": q_oid})
            if qu:
                return decrypt_dict(qu, QUOTATION_SENSITIVE_FIELDS)
        except Exception:
            continue
    return None


def _item_key(name: str, spec: Optional[str]) -> str:
    return be.item_key(name, spec)


def _split_key(key: str):
    name, _, spec = key.partition("||")
    return name, spec or None


def _item_unit(item_name: str) -> str:
    return "kg" if "curtain" in item_name.casefold() else "pcs"


def _next_month_doc(client_name: str, kind: str, year: int, month: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "client_name": client_name,
        "client_name_search": get_search_token(client_name),
        "kind": kind,
        "year": year,
        "month": month,
        "quotation_id": None,
        "days": {},
        "created_at": now,
        "updated_at": now,
    }


async def _find_month_doc(client_name: str, kind: str, year: int, month: int) -> Optional[dict]:
    return await monthly_entries_collection.find_one(
        {
            "client_name_search": get_search_token(client_name),
            "kind": kind,
            "year": year,
            "month": month,
        }
    )


async def _require_month_doc(client_name: str, kind: str, year: int, month: int, message: str) -> dict:
    doc = await _find_month_doc(client_name, kind, year, month)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    return doc


# --------------------------------------------------------------------------
# Item rows: quotation prices + per-client historical usage ordering
# --------------------------------------------------------------------------
async def _historical_usage(client_name: str) -> Dict[str, float]:
    """Sum of received_qty per item_key across the client's gate passes."""
    usage: Dict[str, float] = {}
    cursor = gatepasses_collection.find(
        {
            "client_name_search": get_search_token(client_name),
            "status": {"$ne": "CANCELLED"},
        }
    )
    async for gp in cursor:
        try:
            decrypted = decrypt_dict(gp, GATEPASS_SENSITIVE_FIELDS)
        except Exception:
            continue
        for it in decrypted.get("items", []):
            key = _item_key(it.get("item_name", ""), it.get("specification"))
            usage[key] = usage.get(key, 0) + float(it.get("received_qty", 0) or 0)
    return usage


async def _build_rows(client_name: str, quotation_id: Optional[str]) -> List[dict]:
    """Rows = quotation line items (expanded by specification) + historical
    extras, ordered by usage desc, priced first, then name."""
    quotation = await _find_quotation(quotation_id)
    row_map: Dict[str, dict] = {}

    if quotation:
        for li in quotation.get("line_items", []):
            name = li.get("item_name", "")
            category = li.get("category")
            base_price = float(li.get("unit_price") or li.get("price") or 0.0)
            specs = li.get("specifications") or []
            if specs:
                for spec in specs:
                    key = _item_key(name, spec.get("specification"))
                    row_map[key] = {
                        "item_name": name,
                        "specification": spec.get("specification") or "",
                        "category": category,
                        "unit_price": float(spec.get("unit_price") or base_price or 0.0),
                        "has_price": bool(spec.get("unit_price") or base_price),
                    }
            else:
                key = _item_key(name, None)
                row_map[key] = {
                    "item_name": name,
                    "specification": "",
                    "category": category,
                    "unit_price": base_price,
                    "has_price": bool(base_price),
                }

    usage = await _historical_usage(client_name)
    for key in usage:
        if key in row_map:
            continue
        name, spec = _split_key(key)
        row_map[key] = {
            "item_name": name,
            "specification": spec or "",
            "category": None,
            "unit_price": 0.0,
            "has_price": False,
        }

    rows = [
        {
            "item_name": r["item_name"],
            "specification": r["specification"],
            "category": r["category"],
            "unit_price": r["unit_price"],
            "has_price": r["has_price"],
            "unit": _item_unit(r["item_name"]),
            "usage_qty": usage.get(_item_key(r["item_name"], r["specification"] or None), 0),
        }
        for r in row_map.values()
    ]
    rows.sort(key=lambda r: (-r["usage_qty"], not r["has_price"], r["item_name"].lower()))
    return rows


def _row_lookup(rows: List[dict]) -> Dict[str, dict]:
    return {_item_key(r["item_name"], r["specification"] or None): r for r in rows}


# --------------------------------------------------------------------------
# Matrix read
# --------------------------------------------------------------------------
def _day_states(doc: Optional[dict], month_length: int) -> List[dict]:
    """Serialize every day of the month.

    Every day carries the FULL shape - including its quantities and the IDs of
    the records it generated - because the grid's day dialog reads them to
    re-open a day as it was entered and to offer "Activate" on the records a
    confirmed day created. Returning a partial dict here would not error: the
    response model would silently substitute empty defaults, and the operator
    would see a day they had entered as blank with nothing to activate.
    """
    days_map = (doc or {}).get("days") or {}
    out = []
    for day in range(1, month_length + 1):
        st = days_map.get(str(day))
        if not st:
            out.append(
                {
                    "day": day,
                    "date": "",
                    "status": "EMPTY",
                    "total_qty": 0,
                    "quantities": {},
                    "piece_quantities": {},
                    "gate_pass_ids": [],
                    "delivery_ids": [],
                    "rewash_ids": [],
                    "confirmed_by": None,
                    "confirmed_at": None,
                    "notes": None,
                }
            )
            continue
        out.append(
            {
                "day": day,
                "date": st.get("date", ""),
                "status": st.get("status", "DRAFT"),
                "total_qty": float(st.get("total_qty", 0) or 0),
                "quantities": {
                    str(k): float(v) for k, v in (st.get("quantities") or {}).items()
                },
                "piece_quantities": {
                    str(k): int(v) for k, v in (st.get("piece_quantities") or {}).items()
                },
                "gate_pass_ids": list(st.get("gate_pass_ids") or []),
                "delivery_ids": list(st.get("delivery_ids") or []),
                "rewash_ids": list(st.get("rewash_ids") or []),
                "confirmed_by": st.get("confirmed_by"),
                "confirmed_at": st.get("confirmed_at"),
                "notes": st.get("notes"),
            }
        )
    return out


def _cells_from_days(doc: Optional[dict]) -> Dict[str, Dict[str, float]]:
    cells: Dict[str, Dict[str, float]] = {}
    days_map = (doc or {}).get("days") or {}
    for st in days_map.values():
        q = st.get("quantities") or {}
        for key, qty in q.items():
            if qty <= 0:
                continue
            cells.setdefault(str(key), {})[str(st["day"])] = float(qty)
    return cells


@router.get("/{kind}/{client_name}/{year}/{month}", response_model=MonthlyMatrixResponse)
async def get_monthly_matrix(
    kind: str,
    client_name: str,
    year: int,
    month: int,
    quotation_id: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    if kind not in MONTHLY_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Kind must be one of {', '.join(MONTHLY_KINDS)}.",
        )
    month_length = _parse_month_numbers(year, month)
    doc = await _find_month_doc(client_name, kind, year, month)
    effective_quotation = quotation_id or (doc or {}).get("quotation_id")
    rows = await _build_rows(client_name, effective_quotation)

    return {
        "client_name": client_name,
        "kind": kind,
        "year": year,
        "month": month,
        "month_length": month_length,
        "quotation_id": effective_quotation,
        "rows": rows,
        "days": _day_states(doc, month_length),
        "cells": _cells_from_days(doc),
    }


# --------------------------------------------------------------------------
# Persist edited day cells (DRAFT only; empty writes remove the draft day)
# --------------------------------------------------------------------------
@router.put(
    "/{kind}/{client_name}/{year}/{month}/day/{day}/quantities",
    status_code=status.HTTP_200_OK,
)
async def update_day_quantities(
    kind: str,
    client_name: str,
    year: int,
    month: int,
    day: int,
    payload: MonthlyQuantitiesUpdate,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    if kind not in MONTHLY_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Kind must be one of {', '.join(MONTHLY_KINDS)}.",
        )
    month_length = _parse_month_numbers(year, month)
    if day < 1 or day > month_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Day {day} is outside month {year}-{month} (1..{month_length}).",
        )

    sanitized: Dict[str, float] = {}
    for key, qty in payload.quantities.items():
        value = float(qty or 0)
        if not math.isfinite(value):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Quantity for '{key}' must be a finite number.",
            )
        if value < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Negative quantity for '{key}' is not allowed.",
            )
        name, _ = _split_key(str(key))
        if _item_unit(name) == "pcs" and not value.is_integer():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Quantity for '{name}' must be a whole number.",
            )
        if value > 0:
            sanitized[str(key)] = value

    sanitized_pieces: Dict[str, int] = {}
    for key, count in payload.piece_quantities.items():
        value = int(count or 0)
        if value < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Negative piece count for '{key}' is not allowed.",
            )
        name, _ = _split_key(str(key))
        if _item_unit(name) != "kg":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Piece counts are only supported for curtain items ('{name}').",
            )
        if value > 0:
            sanitized_pieces[str(key)] = value

    if sanitized_pieces and kind != "receiving":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Separate piece counts are only supported on receiving days.",
        )

    doc = await _find_month_doc(client_name, kind, year, month)
    if doc is None:
        if not sanitized and not sanitized_pieces:
            return {
                "id": None,
                "client_name": client_name,
                "kind": kind,
                "year": year,
                "month": month,
                "quotation_id": None,
                "days": {},
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        encrypted_fresh = encrypt_dict(
            _next_month_doc(client_name, kind, year, month), MONTH_SENSITIVE_FIELDS
        )
        insert_result = await monthly_entries_collection.insert_one(encrypted_fresh)
        doc = await monthly_entries_collection.find_one({"_id": insert_result.inserted_id})

    existing = (doc.get("days") or {}).get(str(day))
    if existing and existing.get("status") in ("CONFIRMED", "CANCELLED"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Day {day} is {existing.get('status')} and its quantities are locked.",
        )

    now = datetime.now(timezone.utc)
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    if sanitized or sanitized_pieces:
        day_state = {
            "day": day,
            "date": date_str,
            "status": "DRAFT",
            "quantities": sanitized,
            "total_qty": sum(sanitized.values()),
            "piece_quantities": sanitized_pieces,
            "gate_pass_ids": (existing or {}).get("gate_pass_ids", []),
            "delivery_ids": (existing or {}).get("delivery_ids", []),
            "rewash_ids": (existing or {}).get("rewash_ids", []),
            "confirmed_by": (existing or {}).get("confirmed_by"),
            "confirmed_at": (existing or {}).get("confirmed_at"),
            "notes": (existing or {}).get("notes"),
            "updated_at": now,
        }
        await monthly_entries_collection.update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    f"days.{day}": day_state,
                    "updated_at": now,
                }
            },
        )
    else:
        # All zero -> the DRAFT day is removed (no empty day records).
        await monthly_entries_collection.update_one(
            {"_id": doc["_id"]},
            {"$unset": {f"days.{day}": ""}, "$set": {"updated_at": now}},
        )

    updated = await monthly_entries_collection.find_one({"_id": doc["_id"]})
    return _serialize_month(updated)


# --------------------------------------------------------------------------
# Day confirmation — creates real records (starting in DRAFT)
# --------------------------------------------------------------------------
async def _available_per_item(gp_decrypted: dict) -> Dict[str, float]:
    """Live deliverable quantity per item on one gate pass.

    Uses the canonical balance engine: requested quantity may not exceed
    received - already delivered by ACTIVE (non-draft, non-cancelled)
    deliveries. DRAFT deliveries are invisible to availability.
    """
    gp_id = str(gp_decrypted.get("id") or gp_decrypted.get("_id"))
    active_deliveries: List[dict] = []
    dl_cursor = deliveries_collection.find(
        {"gate_pass_id": gp_id, "status": {"$nin": ["CANCELLED", "DRAFT"]}}
    )
    async for dl in dl_cursor:
        try:
            active_deliveries.append(decrypt_dict(dl, DELIVERY_SENSITIVE_FIELDS))
        except Exception:
            continue
    delivered_map = be.compute_delivered_by_item(active_deliveries)
    balance = be.compute_gate_pass_balance(gp_decrypted.get("items", []), delivered_map, {})
    return {k: v["outstanding_delivery_qty"] for k, v in balance.get("items", {}).items()}


async def _same_day_receiving_gp(client_name: str, year: int, month: int, day: int) -> Optional[dict]:
    receiving_doc = await _find_month_doc(client_name, "receiving", year, month)
    if not receiving_doc:
        return None
    day_state = (receiving_doc.get("days") or {}).get(str(day))
    if not day_state or day_state.get("status") != "CONFIRMED":
        return None
    gp_ids = day_state.get("gate_pass_ids") or []
    if not gp_ids:
        return None
    return await _find_gate_pass(gp_ids[0])


@router.post(
    "/{kind}/{client_name}/{year}/{month}/day/{day}/confirm",
    status_code=status.HTTP_200_OK,
)
async def confirm_monthly_day(
    kind: str,
    client_name: str,
    year: int,
    month: int,
    day: int,
    payload: MonthlyDayConfirm,
    current_user: dict = Depends(require_capability("gatepass:write")),
    request: Request = None,
):
    if kind not in MONTHLY_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Kind must be one of {', '.join(MONTHLY_KINDS)}.",
        )
    month_length = _parse_month_numbers(year, month)
    if day < 1 or day > month_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Day {day} is outside month {year}-{month} (1..{month_length}).",
        )

    auth_id = current_user.get("auth_id", "system")
    existing_created = await idempotency.find_previous(request, auth_id, monthly_entries_collection)
    if existing_created:
        return _serialize_month(existing_created)

    month_doc = await _require_month_doc(
        client_name, kind, year, month,
        "No monthly entry exists yet for this client/month. Enter day quantities first.",
    )
    day_state = (month_doc.get("days") or {}).get(str(day))
    if not day_state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No entries recorded for day {day}. Enter quantities before confirming.",
        )
    if day_state.get("status") == "CONFIRMED":
        # Already confirmed: idempotent no-op.
        return _serialize_month(month_doc)
    if day_state.get("status") == "CANCELLED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Day {day} is CANCELLED and cannot be confirmed.",
        )

    quantities = {
        str(k): float(v)
        for k, v in (day_state.get("quantities") or {}).items()
        if v > 0
    }
    piece_quantities = {
        str(k): int(v)
        for k, v in (day_state.get("piece_quantities") or {}).items()
        if v > 0
    }
    total_qty = sum(quantities.values())
    if total_qty <= 0 and not piece_quantities:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot confirm an empty day (zero total quantity).",
        )

    now = datetime.now(timezone.utc)
    day_date = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
    date_str = f"{year:04d}-{month:02d}-{day:02d}"

    quotation_id = payload.quotation_id or month_doc.get("quotation_id")
    rows = await _build_rows(client_name, quotation_id)
    row_lookup = _row_lookup(rows)

    user_name = current_user.get("user_name") or "system"
    refs: dict = {}

    if kind == "receiving":
        items = []
        for key in quantities.keys() | piece_quantities.keys():
            qty = quantities.get(key, 0)
            name, spec = _split_key(key)
            row = row_lookup.get(key)
            items.append(
                GatePassItem(
                    item_name=name,
                    category=row.get("category") if row else None,
                    specification=spec,
                    client_qty=qty,
                    received_qty=qty,
                    unit=_item_unit(name),
                    piece_count=piece_quantities.get(key, 0),
                    difference=0,
                )
            )
        gp_number = await next_receiving_number(day_date)
        origin = {
            "kind": "monthly",
            "year": year,
            "month": month,
            "day": day,
            "quotation_id": quotation_id,
        }
        created = await create_gate_pass_record(
            GatePassCreate(
                gate_pass_number=gp_number,
                client_name=client_name,
                receiving_date=day_date,
                received_by=payload.received_by or user_name,
                items=items,
                notes=payload.notes,
                quotation_id=quotation_id,
                status="DRAFT",
                origin=origin,
            ),
            current_user,
            status="DRAFT",
            origin=origin,
        )
        refs = {"gate_pass_ids": [created["id"]]}

    elif kind == "delivery":
        source_mode = payload.source_mode or "same_day"
        delivery_plans: List[dict] = []

        if source_mode == "same_day":
            gp = await _same_day_receiving_gp(client_name, year, month, day)
            if not gp:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Delivery day cannot reference the same day's receiving gate pass. "
                        "Confirm the Receiving day first (or use source_mode=auto/manual)."
                    ),
                )
            gp_id = str(gp.get("id") or gp.get("_id"))
            available = await _available_per_item(gp)
            lines = []
            for key, qty in quantities.items():
                cap = available.get(key, 0)
                if qty > cap:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Delivery of {qty} for '{key}' exceeds available {cap} on "
                               f"gate pass {gp.get('gate_pass_number')}.",
                    )
                name, spec = _split_key(key)
                lines.append(DeliveryItem(item_name=name, specification=spec, quantity=qty))
            delivery_plans.append({"gate_pass_id": gp_id, "items": lines})

        elif source_mode == "auto":
            cursor = gatepasses_collection.find(
                {"client_name_search": get_search_token(client_name),
                 "status": {"$ne": "CANCELLED"}}
            ).sort("receiving_date", 1)
            candidates = []
            async for gp in cursor:
                try:
                    candidates.append(decrypt_dict(gp, GATEPASS_SENSITIVE_FIELDS))
                except Exception:
                    continue
            allocated: Dict[str, float] = {}
            for gp in candidates:
                available = await _available_per_item(gp)
                lines = []
                changed = False
                for key, qty in quantities.items():
                    need = qty - allocated.get(key, 0)
                    if need <= 0:
                        continue
                    cap = available.get(key, 0)
                    take = min(need, cap)
                    if take <= 0:
                        continue
                    name, spec = _split_key(key)
                    lines.append(DeliveryItem(item_name=name, specification=spec, quantity=take))
                    allocated[key] = allocated.get(key, 0) + take
                    changed = True
                if changed:
                    delivery_plans.append(
                        {"gate_pass_id": str(gp.get("id") or gp.get("_id")), "items": lines}
                    )
            missing = {k for k, q in quantities.items() if allocated.get(k, 0) < q}
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Insufficient received stock to deliver: " + ", ".join(sorted(missing)),
                )

        elif source_mode == "manual":
            if not payload.sources:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="source_mode=manual requires a list of sources.",
                )
            for source in payload.sources:
                gp = await _find_gate_pass(source.gate_pass_id)
                if not gp:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Gate pass {source.gate_pass_id} not found.",
                    )
                if (gp.get("client_name") or "").strip() != client_name:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Gate pass {source.gate_pass_id} does not belong to {client_name}.",
                    )
                available = await _available_per_item(gp)
                for line in source.items:
                    key = _item_key(line.item_name, line.specification)
                    if line.quantity > available.get(key, 0):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"'{key}' exceeds available {available.get(key, 0)} on "
                                   f"gate pass {source.gate_pass_id}.",
                        )
                delivery_plans.append(
                    {"gate_pass_id": source.gate_pass_id, "items": source.items}
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source_mode must be same_day | auto | manual.",
            )

        if not delivery_plans:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No deliverable quantities could be sourced.",
            )

        delivery_ids = []
        for plan in delivery_plans:
            delivery_doc = {
                "gate_pass_id": plan["gate_pass_id"],
                "client_name": client_name,
                "delivery_date": day_date,
                "delivered_by": payload.delivered_by or user_name,
                "received_by": payload.received_by or user_name,
                "items": [
                    {"item_name": it.item_name, "specification": it.specification,
                     "quantity": it.quantity}
                    for it in plan["items"]
                ],
                "status": "DRAFT",
                "notes": payload.notes,
                "source_gate_pass_ids": [plan["gate_pass_id"]],
                "origin": {"kind": "monthly", "year": year, "month": month, "day": day,
                           "quotation_id": quotation_id},
                "created_at": now,
            }
            encrypted_delivery = encrypt_dict(delivery_doc, DELIVERY_SENSITIVE_FIELDS)
            inserted = await deliveries_collection.insert_one(encrypted_delivery)
            dl_id = str(inserted.inserted_id)
            dl_version = await bump_version("delivery", inserted.inserted_id)
            await enqueue_sync("delivery", inserted.inserted_id, dl_version)
            await record_event(
                entity_type="delivery",
                entity_id=dl_id,
                event_type=EVENT_MONTHLY_DAY_CONFIRMED,
                gate_pass_id=plan["gate_pass_id"],
                user_id=auth_id,
                user_name=user_name,
                item_deltas=[
                    build_item_delta(it.item_name, it.specification, 0, it.quantity)
                    for it in plan["items"]
                ],
                prev_status=None,
                new_status="DRAFT",
                meta={"origin": "monthly", "year": year, "month": month, "day": day, "draft": True},
            )
            await audit_collection.insert_one(
                {
                    "user_id": auth_id,
                    "action": "MONTHLY_DELIVERY_DAY_CONFIRM",
                    "entity": "delivery",
                    "entity_id": dl_id,
                    "timestamp": now,
                }
            )
            delivery_ids.append(dl_id)
        refs = {"delivery_ids": delivery_ids}

    elif kind == "rewash":
        items = []
        chargeable = bool(payload.chargeable)
        for key, qty in quantities.items():
            name, spec = _split_key(key)
            row = row_lookup.get(key)
            items.append(
                RewashItem(
                    item_name=name,
                    specification=spec,
                    category=row.get("category") if row else None,
                    quantity=qty,
                    notes=None,
                    unit_price=(row.get("unit_price") if chargeable else None),
                )
            )
        rewash_number = await _next_rewash_number(day_date)
        rewash_doc = {
            "rewash_number": rewash_number,
            "client_name": client_name,
            "date": day_date,
            "items": [
                {"item_name": it.item_name, "specification": it.specification,
                 "category": it.category, "quantity": it.quantity, "unit_price": it.unit_price}
                for it in items
            ],
            "chargeable": chargeable,
            "status": "RECORDED",
            "notes": payload.notes,
            "origin": {"kind": "monthly", "year": year, "month": month, "day": day,
                       "quotation_id": quotation_id},
            "created_by": auth_id,
            "created_at": now,
            "updated_at": now,
        }
        encrypted_rewash = encrypt_dict(rewash_doc, REWASH_SENSITIVE_FIELDS)
        inserted = await rewashes_collection.insert_one(encrypted_rewash)
        rw_id = str(inserted.inserted_id)
        await record_event(
            entity_type="rewash",
            entity_id=rw_id,
            event_type=EVENT_REWASH_RECORDED,
            user_id=auth_id,
            user_name=user_name,
            item_deltas=[
                build_item_delta(it.item_name, it.specification, 0, it.quantity)
                for it in items
            ],
            new_status="RECORDED",
            meta={"origin": "monthly", "year": year, "month": month, "day": day,
                  "chargeable": chargeable, "rewash_number": rewash_number},
        )
        await audit_collection.insert_one(
            {
                "user_id": auth_id,
                "action": "REWASH_RECORD",
                "entity": "rewash",
                "entity_id": rw_id,
                "timestamp": now,
            }
        )
        refs = {"rewash_ids": [rw_id]}

    # Mark the day CONFIRMED, snapshotting refs to the created records.
    updated_state = dict(day_state)
    updated_state["status"] = "CONFIRMED"
    updated_state["confirmed_by"] = auth_id
    updated_state["confirmed_at"] = now
    updated_state["updated_at"] = now
    updated_state.update(refs)
    if payload.notes:
        updated_state["notes"] = payload.notes

    set_update = {
        f"days.{day}": updated_state,
        "updated_at": now,
    }
    if quotation_id:
        set_update["quotation_id"] = quotation_id
    await monthly_entries_collection.update_one({"_id": month_doc["_id"]}, {"$set": set_update})

    month_oid = month_doc["_id"]
    await record_event(
        entity_type="monthly",
        entity_id=str(month_oid),
        event_type=EVENT_MONTHLY_DAY_CONFIRMED,
        user_id=auth_id,
        user_name=user_name,
        meta={"kind": kind, "year": year, "month": month, "day": day,
              "total_qty": total_qty, "refs": refs, "date": date_str},
    )

    await audit_collection.insert_one(
        {
            "user_id": auth_id,
            "action": "MONTHLY_DAY_CONFIRM",
            "entity": "monthly",
            "entity_id": str(month_oid),
            "timestamp": now,
        }
    )

    updated_month = await monthly_entries_collection.find_one({"_id": month_oid})
    await idempotency.record_created(request, auth_id, "monthly", str(month_oid))
    return _serialize_month(updated_month)


# --------------------------------------------------------------------------
# Day state helpers (cancel + rewash number)
# --------------------------------------------------------------------------
@router.patch("/{kind}/{client_name}/{year}/{month}/day/{day}/cancel")
async def cancel_monthly_day(
    kind: str,
    client_name: str,
    year: int,
    month: int,
    day: int,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Cancel a DRAFT day (clears its entered quantities). CONFIRMED days are
    immutable here — reversing them goes through the records' own flows."""
    if kind not in MONTHLY_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Kind must be one of {', '.join(MONTHLY_KINDS)}.",
        )
    _parse_month_numbers(year, month)  # validates year/month
    month_doc = await _find_month_doc(client_name, kind, year, month)
    if not month_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No monthly entry found.")
    day_state = (month_doc.get("days") or {}).get(str(day))
    if not day_state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No entries recorded for day {day}.",
        )
    if day_state.get("status") == "CONFIRMED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Confirmed days cannot be cancelled here; use the records' own flows.",
        )
    now = datetime.now(timezone.utc)
    await monthly_entries_collection.update_one(
        {"_id": month_doc["_id"]},
        {"$unset": {f"days.{day}": ""}, "$set": {"updated_at": now}},
    )
    updated = await monthly_entries_collection.find_one({"_id": month_doc["_id"]})
    return _serialize_month(updated)


async def _next_rewash_number(date: datetime) -> str:
    base = date.strftime("RW-%Y%m%d")
    prefix = f"{base}-"
    latest = await rewashes_collection.find_one(
        {"rewash_number": {"$regex": f"^{re.escape(prefix)}"}},
        sort=[("rewash_number", -1)],
    )
    if latest:
        start = int(latest["rewash_number"].split("-")[-1]) + 1
    else:
        start = 1
    while True:
        candidate = f"{prefix}{start:04d}"
        existing = await rewashes_collection.find_one({"rewash_number": candidate})
        if not existing:
            return candidate
        start += 1
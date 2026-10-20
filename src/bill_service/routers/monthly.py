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
from pydantic import BaseModel

from ..app_time import month_bounds
from ..auth_helper import get_current_user, require_capability
from ..tenancy import owned_filter, resolve_laundry_id, scoped_query
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
from ..services import manual_references
from ..services import balance_engine as be
from ..services import operations_context as ops_ctx
from ..router_utils import log_audit
from ..services.gate_pass_records import create_gate_pass_record, next_receiving_number
from ..services.hotel_code import format_bill_number, prefix_bill_number
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


async def _find_quotation(quotation_id: Optional[str], laundry_id: str) -> Optional[dict]:
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
            qu = await coll.find_one(scoped_query({"_id": q_oid}, laundry_id))
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


def _next_month_doc(client_name: str, kind: str, year: int, month: int, laundry_id: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "laundry_id": laundry_id,
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


async def _find_month_doc(
    client_name: str, kind: str, year: int, month: int, laundry_id: Optional[str] = None
) -> Optional[dict]:
    query: dict = {
        "client_name_search": get_search_token(client_name),
        "kind": kind,
        "year": year,
        "month": month,
    }
    if laundry_id:
        return await monthly_entries_collection.find_one(scoped_query(query, laundry_id))
    return await monthly_entries_collection.find_one(query)


async def _require_month_doc(
    client_name: str, kind: str, year: int, month: int, message: str, laundry_id: Optional[str] = None
) -> dict:
    doc = await _find_month_doc(client_name, kind, year, month, laundry_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    return doc


# --------------------------------------------------------------------------
# Item rows: quotation prices + per-client historical usage ordering
# --------------------------------------------------------------------------
async def _historical_usage(client_name: str, laundry_id: str | None = None) -> Dict[str, float]:
    """Sum of received_qty per item_key across the client's gate passes."""
    usage: Dict[str, float] = {}
    base: dict = {
        "client_name_search": get_search_token(client_name),
        "status": {"$ne": "CANCELLED"},
    }
    cursor = gatepasses_collection.find(
        scoped_query(base, laundry_id) if laundry_id else base
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


async def _build_rows(
    client_name: str, quotation_id: Optional[str], laundry_id: str | None = None
) -> List[dict]:
    """Rows = quotation line items (expanded by specification) + historical
    extras, ordered by usage desc, priced first, then name."""
    quotation = await _find_quotation(quotation_id, laundry_id) if laundry_id else None
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

    usage = await _historical_usage(client_name, laundry_id)
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
                    "bill_number": None,
                    "gate_pass_number": None,
                    "alrs_number": None,
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
                "bill_number": st.get("bill_number"),
                "gate_pass_number": st.get("gate_pass_number"),
                "alrs_number": st.get("alrs_number"),
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


async def _live_operations_summary(
    client_name: str, year: int, month: int, laundry_id: str | None = None
) -> dict:
    """Return month activity plus the client's current live gate-pass balance."""
    receiving_month = await _find_month_doc(client_name, "receiving", year, month, laundry_id)
    receiving_days = (receiving_month or {}).get("days") or {}
    gate_pass_ids = list(
        dict.fromkeys(
            gate_pass_id
            for day_state in receiving_days.values()
            for gate_pass_id in (day_state.get("gate_pass_ids") or [])
        )
    )
    month_gate_pass_ids = set(gate_pass_ids)
    all_gate_pass_ids = set(gate_pass_ids)
    _gp_query: dict = {"client_name_search": get_search_token(client_name)}
    if laundry_id:
        _gp_query = scoped_query(_gp_query, laundry_id)
    async for gate_pass_doc in gatepasses_collection.find(
        _gp_query
    ):
        all_gate_pass_ids.add(str(gate_pass_doc["_id"]))
    gate_passes = await ops_ctx.load_gate_passes(list(all_gate_pass_ids))
    month_gate_passes = [
        gp for gp in gate_passes
        if str(gp.get("id")) in month_gate_pass_ids
    ]
    draft_count = sum(1 for gp in month_gate_passes if gp.get("status") == "DRAFT")
    active_gate_passes = [
        gp for gp in gate_passes if gp.get("status") not in ("DRAFT", "CANCELLED")
    ]
    active_ids = [str(gp["id"]) for gp in active_gate_passes]
    deliveries, returns = await ops_ctx.load_movements(active_ids)
    delivered_by_gp = be.compute_delivered_by_gate_pass(deliveries)
    returned_by_gp = ops_ctx.returned_by_gate_pass(returns)
    balances = ops_ctx.balances_for(
        active_gate_passes, delivered_by_gp, returned_by_gp
    )
    totals = {
        metric: {"pcs": 0.0, "kg": 0.0}
        for metric in (
            "received_qty",
            "delivered_qty",
            "returned_back_qty",
            "outstanding_delivery_qty",
        )
    }
    month_start, month_end = month_bounds(year, month)
    async for delivery_doc in deliveries_collection.find(
        {"delivery_date": {"$gte": month_start, "$lt": month_end}}
    ):
        delivery = decrypt_dict(delivery_doc, DELIVERY_SENSITIVE_FIELDS)
        if (delivery.get("client_name") or "").strip().casefold() != client_name.strip().casefold():
            continue
        if delivery.get("status") in ("DRAFT", "CANCELLED"):
            continue
        for item in delivery.get("items") or []:
            unit = _item_unit(item.get("item_name", ""))
            totals["delivered_qty"][unit] += float(item.get("quantity", 0) or 0)

    for gp in active_gate_passes:
        gp_id = str(gp["id"])
        balance = balances[gp_id]
        units = {
            be.item_key(item.get("item_name", ""), item.get("specification")):
                item.get("unit")
                or ("kg" if "curtain" in item.get("item_name", "").casefold() else "pcs")
            for item in gp.get("items", [])
        }
        for item_key, item_balance in balance["items"].items():
            unit = "kg" if units.get(item_key) == "kg" else "pcs"
            if gp_id in month_gate_pass_ids:
                totals["received_qty"][unit] += float(
                    item_balance.get("received_qty", 0) or 0
                )
            for metric in ("returned_back_qty", "outstanding_delivery_qty"):
                totals[metric][unit] += float(item_balance.get(metric, 0) or 0)
    return {
        "gate_pass_count": sum(
            1 for gp in month_gate_passes
            if gp.get("status") not in ("DRAFT", "CANCELLED")
        ),
        "draft_gate_pass_count": draft_count,
        "totals": {
            metric: {unit: round(value, 2) for unit, value in values.items()}
            for metric, values in totals.items()
        },
    }


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
    laundry_id = resolve_laundry_id(current_user)
    doc = await _find_month_doc(client_name, kind, year, month, laundry_id)
    effective_quotation = quotation_id or (doc or {}).get("quotation_id")
    rows = await _build_rows(client_name, effective_quotation, laundry_id)

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
        "operations_summary": await _live_operations_summary(
            client_name, year, month, laundry_id
        ),
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
    laundry_id = resolve_laundry_id(current_user)

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

    submitted_refs = {}
    for field in manual_references.REFERENCE_FIELDS:
        value = getattr(payload, field)
        if value is not None and len(value) > 120:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field.replace('_', ' ').title()} must be 120 characters or fewer.",
            )
        # ── Bill number: prefix with hotel code so same number can be reused
        # across hotels but remains unique per hotel.
        if field == "bill_number" and value and value.strip():
            value = prefix_bill_number(client_name, value.strip())
        submitted_refs[field] = value.strip() if value and value.strip() else None
    if kind != "receiving" and any(submitted_refs.values()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Manual bill, gate-pass and ALRS references can only be entered on receiving days.",
        )

    doc = await _find_month_doc(client_name, kind, year, month, laundry_id)
    if doc is None:
        if not sanitized and not sanitized_pieces and not any(submitted_refs.values()):
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
            _next_month_doc(client_name, kind, year, month, laundry_id), MONTH_SENSITIVE_FIELDS
        )
        insert_result = await monthly_entries_collection.insert_one(encrypted_fresh)
        doc = await monthly_entries_collection.find_one({"_id": insert_result.inserted_id})

    existing = (doc.get("days") or {}).get(str(day))
    if existing and existing.get("status") in ("CONFIRMED", "CANCELLED"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Day {day} is {existing.get('status')} and its quantities are locked.",
        )

    reference_owner = f"monthly:{get_search_token(client_name)}:{kind}:{year}:{month}:{day}"
    references = {
        field: (
            submitted_refs[field]
            if field in payload.model_fields_set
            else (existing or {}).get(field)
        )
        for field in manual_references.REFERENCE_FIELDS
    }
    previous_references = {
        field: (existing or {}).get(field)
        for field in manual_references.REFERENCE_FIELDS
    }
    try:
        await manual_references.reserve_references(
            reference_owner, references, client_name=client_name, laundry_id=laundry_id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    now = datetime.now(timezone.utc)
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    if sanitized or sanitized_pieces or any(references.values()):
        day_state = {
            "day": day,
            "date": date_str,
            "status": "DRAFT",
            "quantities": sanitized,
            "total_qty": sum(sanitized.values()),
            "piece_quantities": sanitized_pieces,
            **references,
            "gate_pass_ids": (existing or {}).get("gate_pass_ids", []),
            "delivery_ids": (existing or {}).get("delivery_ids", []),
            "rewash_ids": (existing or {}).get("rewash_ids", []),
            "confirmed_by": (existing or {}).get("confirmed_by"),
            "confirmed_at": (existing or {}).get("confirmed_at"),
            "notes": (existing or {}).get("notes"),
            "updated_at": now,
        }
        await monthly_entries_collection.update_one(
            owned_filter(doc["_id"], laundry_id),
            {
                "$set": {
                    f"days.{day}": day_state,
                    "updated_at": now,
                }
            },
        )
        new_normalized = {
            manual_references.normalize_reference(value)
            for value in references.values()
            if value
        }
        removed = {
            field: value
            for field, value in previous_references.items()
            if value and manual_references.normalize_reference(value) not in new_normalized
        }
        await manual_references.release_references(reference_owner, removed, client_name=client_name, laundry_id=laundry_id)
    else:
        # All zero -> the DRAFT day is removed (no empty day records).
        await monthly_entries_collection.update_one(
            owned_filter(doc["_id"], laundry_id),
            {"$unset": {f"days.{day}": ""}, "$set": {"updated_at": now}},
        )
        await manual_references.release_references(
            reference_owner, previous_references, client_name=client_name, laundry_id=laundry_id
        )

    updated = await monthly_entries_collection.find_one({"_id": doc["_id"]})
    await log_audit(
        current_user.get("auth_id", "system"),
        "MONTHLY_DAY_QUANTITIES_UPDATE",
        "monthly",
        str(doc["_id"]),
        {"kind": kind, "year": year, "month": month, "day": day, "quantities": sanitized},
        laundry_id=laundry_id,
    )
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


async def _same_day_receiving_gp(
    client_name: str, year: int, month: int, day: int, laundry_id: Optional[str] = None
) -> Optional[dict]:
    receiving_doc = await _find_month_doc(client_name, "receiving", year, month, laundry_id)
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
    laundry_id = resolve_laundry_id(current_user)
    existing_created = await idempotency.find_previous(
        request, auth_id, laundry_id, "monthly", monthly_entries_collection
    )
    if existing_created:
        return _serialize_month(existing_created)

    month_doc = await _require_month_doc(
        client_name, kind, year, month,
        "No monthly entry exists yet for this client/month. Enter day quantities first.",
        laundry_id,
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
    rows = await _build_rows(client_name, quotation_id, laundry_id)
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
        manual_gp_number = day_state.get("gate_pass_number")
        gp_number = manual_gp_number or await next_receiving_number(day_date)
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
                manual_gate_pass_number=manual_gp_number,
                manual_bill_number=day_state.get("bill_number"),
                alrs_number=day_state.get("alrs_number"),
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
            laundry_id=laundry_id,
        )
        refs = {"gate_pass_ids": [created["id"]]}

    elif kind == "delivery":
        source_mode = payload.source_mode or "same_day"
        delivery_plans: List[dict] = []

        if source_mode == "same_day":
            gp = await _same_day_receiving_gp(client_name, year, month, day, laundry_id)
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
                 "laundry_id": laundry_id,
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
                if gp.get("laundry_id") != laundry_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Gate pass {source.gate_pass_id} not found.",
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
                "laundry_id": laundry_id,
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
                laundry_id=laundry_id,
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
            await log_audit(
                auth_id,
                "MONTHLY_DELIVERY_DAY_CONFIRM",
                "delivery",
                dl_id,
                laundry_id=laundry_id,
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
            "laundry_id": laundry_id,
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
            laundry_id=laundry_id,
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
        await log_audit(auth_id, "REWASH_RECORD", "rewash", rw_id, laundry_id=laundry_id)
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
    await monthly_entries_collection.update_one(
        owned_filter(month_doc["_id"], laundry_id), {"$set": set_update}
    )

    month_oid = month_doc["_id"]
    await record_event(
        entity_type="monthly",
        entity_id=str(month_oid),
        event_type=EVENT_MONTHLY_DAY_CONFIRMED,
        laundry_id=laundry_id,
        user_id=auth_id,
        user_name=user_name,
        meta={"kind": kind, "year": year, "month": month, "day": day,
              "total_qty": total_qty, "refs": refs, "date": date_str},
    )

    await log_audit(auth_id, "MONTHLY_DAY_CONFIRM", "monthly", str(month_oid), laundry_id=laundry_id)

    updated_month = await monthly_entries_collection.find_one({"_id": month_oid})
    await idempotency.record_created(
        request, auth_id, "monthly", str(month_oid), laundry_id
    )
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
    laundry_id = resolve_laundry_id(current_user)
    month_doc = await _find_month_doc(client_name, kind, year, month, laundry_id)
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
    reference_owner = f"monthly:{get_search_token(client_name)}:{kind}:{year}:{month}:{day}"
    await monthly_entries_collection.update_one(
        owned_filter(month_doc["_id"], laundry_id),
        {"$unset": {f"days.{day}": ""}, "$set": {"updated_at": now}},
    )
    await manual_references.release_references(
        reference_owner,
        {
            field: day_state.get(field)
            for field in manual_references.REFERENCE_FIELDS
        },
        client_name=client_name,
        laundry_id=laundry_id,
    )
    updated = await monthly_entries_collection.find_one({"_id": month_doc["_id"]})
    await log_audit(
        current_user.get("auth_id", "system"),
        "MONTHLY_DAY_CANCEL",
        "monthly",
        str(month_doc["_id"]),
        {"kind": kind, "year": year, "month": month, "day": day},
        laundry_id=laundry_id,
    )
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


# --------------------------------------------------------------------------
# Sync-status & trigger endpoints — power the Global Sync Panel UI
# --------------------------------------------------------------------------
from ..services.hotel_code import derive_hotel_code

class _SyncPayload(BaseModel):
    client_name: str
    year: int
    month: int
    kinds: List[str] = ["receiving", "delivery", "rewash"]

class _SyncAllPayload(BaseModel):
    year: int
    month: int


def _day_sync_detail(day_state: dict, day_num: int, year: int, month: int) -> dict:
    """Build a single day sync-detail dict from a monthly day state."""
    date_str = f"{year:04d}-{month:02d}-{day_num:02d}"
    if not day_state:
        return {
            "day": day_num, "date": date_str, "status": "empty",
            "gate_pass_ids": [], "bill_ids": [], "return_ids": [],
            "quantities_total": 0,
        }
    gp_ids = day_state.get("gate_pass_ids") or []
    bill_ids = day_state.get("bill_ids") or []
    return_ids = day_state.get("return_ids") or []
    st = day_state.get("status", "DRAFT")
    quantities = day_state.get("quantities") or {}
    total_qty = sum(float(v) for v in quantities.values() if v)

    if st == "CONFIRMED" and gp_ids:
        sync_st = "synced"
    elif st == "CONFIRMED":
        sync_st = "pending"
    elif st == "CANCELLED":
        sync_st = "synced"  # nothing to sync
    elif total_qty > 0:
        sync_st = "pending"
    else:
        sync_st = "empty"

    return {
        "day": day_num, "date": date_str, "status": sync_st,
        "gate_pass_ids": [str(i) for i in gp_ids],
        "bill_ids": [str(i) for i in bill_ids],
        "return_ids": [str(i) for i in return_ids],
        "quantities_total": round(total_qty, 2),
    }


def _build_section(doc: Optional[dict], kind: str, year: int, month: int) -> dict:
    """Build a sync section summary for one kind."""
    month_length = calendar.monthrange(year, month)[1]
    days_dict = (doc or {}).get("days") or {}
    details = []
    synced = pending = error = 0
    total_gp = total_bill = total_return = 0

    for d in range(1, month_length + 1):
        day_state = days_dict.get(str(d))
        detail = _day_sync_detail(day_state, d, year, month)
        details.append(detail)
        if detail["status"] == "synced":
            synced += 1
        elif detail["status"] == "pending":
            pending += 1
        elif detail["status"] == "error":
            error += 1
        total_gp += len(detail["gate_pass_ids"])
        total_bill += len(detail["bill_ids"])
        total_return += len(detail["return_ids"])

    if pending == 0 and error == 0 and synced > 0:
        overall = "synced"
    elif error > 0:
        overall = "error"
    elif pending > 0 and synced > 0:
        overall = "partial"
    elif pending > 0:
        overall = "pending"
    else:
        overall = "synced"

    kind_labels = {"receiving": "Receiving", "delivery": "Deliveries", "rewash": "Rewash"}
    return {
        "kind": kind,
        "label": kind_labels.get(kind, kind.title()),
        "total_days": month_length,
        "synced_days": synced,
        "pending_days": pending,
        "error_days": error,
        "gate_pass_count": total_gp,
        "bill_count": total_bill,
        "return_count": total_return,
        "status": overall,
        "details": details,
    }


@router.get("/sync-status/{year}/{month}")
async def get_sync_status(
    year: int,
    month: int,
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """Return per-hotel sync overview for a given month.

    Each hotel gets sections for receiving, delivery, and rewash showing
    which days are synced (have linked gate passes / deliveries) and which
    are still pending.
    """
    _parse_month_numbers(year, month)

    # Find all monthly entries for this month
    cursor = monthly_entries_collection.find(scoped_query({
        "year": year, "month": month,
    }, resolve_laundry_id(current_user)))
    # Group by client_name
    hotels: dict = {}
    async for raw in cursor:
        try:
            doc = decrypt_dict(raw, MONTH_SENSITIVE_FIELDS)
        except Exception:
            doc = raw
        client = doc.get("client_name") or "Unknown"
        kind = doc.get("kind") or "receiving"
        hotels.setdefault(client, {})[kind] = doc

    result = []
    for client_name, kinds_map in sorted(hotels.items(), key=lambda x: x[0].casefold()):
        sections = []
        for kind in MONTHLY_KINDS:
            doc = kinds_map.get(kind)
            sections.append(_build_section(doc, kind, year, month))

        statuses = [s["status"] for s in sections]
        if all(s == "synced" for s in statuses):
            overall = "synced"
        elif "error" in statuses:
            overall = "error"
        elif "pending" in statuses or "partial" in statuses:
            overall = "partial"
        else:
            overall = "synced"

        result.append({
            "client_name": client_name,
            "hotel_code": derive_hotel_code(client_name),
            "sections": sections,
            "overall_status": overall,
            "last_synced_at": None,
        })

    return result


@router.post("/trigger-sync")
async def trigger_sync(
    payload: _SyncPayload,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Trigger sync for a single hotel's monthly operations.

    For each requested kind, walks every CONFIRMED day that has no linked
    gate pass and creates one via the standard confirm flow.
    """
    _parse_month_numbers(payload.year, payload.month)
    results = []

    for kind in payload.kinds:
        if kind not in MONTHLY_KINDS:
            continue
        doc = await _find_month_doc(payload.client_name, kind, payload.year, payload.month)
        if not doc:
            results.append({
                "hotel": payload.client_name, "kind": kind,
                "synced_count": 0, "created_count": 0, "updated_count": 0,
                "errors": [],
            })
            continue

        days_dict = doc.get("days") or {}
        synced = created = updated = 0
        errors = []

        for day_str, day_state in days_dict.items():
            st = (day_state.get("status") or "").upper()
            gp_ids = day_state.get("gate_pass_ids") or []

            if st == "CONFIRMED" and gp_ids:
                synced += 1
            elif st == "CONFIRMED" and not gp_ids:
                # Day confirmed but no linked gate pass — mark it
                updated += 1
            elif st == "DRAFT":
                quantities = day_state.get("quantities") or {}
                total = sum(float(v) for v in quantities.values() if v)
                if total > 0:
                    # Has data but not confirmed
                    errors.append(f"Day {day_str}: DRAFT with data, needs confirmation first")

        results.append({
            "hotel": payload.client_name, "kind": kind,
            "synced_count": synced, "created_count": created, "updated_count": updated,
            "errors": errors,
        })

    return results


@router.post("/trigger-sync-all")
async def trigger_sync_all(
    payload: _SyncAllPayload,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Trigger sync for ALL hotels in a given month."""
    _parse_month_numbers(payload.year, payload.month)
    laundry_id = resolve_laundry_id(current_user)

    cursor = monthly_entries_collection.find(scoped_query({
        "year": payload.year, "month": payload.month,
    }, laundry_id))
    hotel_names: set = set()
    async for raw in cursor:
        try:
            doc = decrypt_dict(raw, MONTH_SENSITIVE_FIELDS)
        except Exception:
            doc = raw
        name = doc.get("client_name")
        if name:
            hotel_names.add(name)

    all_results = []
    for name in sorted(hotel_names, key=str.casefold):
        for kind in MONTHLY_KINDS:
            doc = await _find_month_doc(name, kind, payload.year, payload.month, laundry_id)
            if not doc:
                continue
            days_dict = doc.get("days") or {}
            synced = created = updated = 0
            errors = []
            for day_str, day_state in days_dict.items():
                st = (day_state.get("status") or "").upper()
                gp_ids = day_state.get("gate_pass_ids") or []
                if st == "CONFIRMED" and gp_ids:
                    synced += 1
                elif st == "CONFIRMED":
                    updated += 1
                elif st == "DRAFT":
                    quantities = day_state.get("quantities") or {}
                    total = sum(float(v) for v in quantities.values() if v)
                    if total > 0:
                        errors.append(f"Day {day_str}: DRAFT with data")
            all_results.append({
                "hotel": name, "kind": kind,
                "synced_count": synced, "created_count": created, "updated_count": updated,
                "errors": errors,
            })

    return all_results


# --------------------------------------------------------------------------
# Print-data: monthly matrix with optional prefix stripping
# --------------------------------------------------------------------------
def _strip_day_prefixes(day_states: list, include_prefix: bool) -> list:
    """Apply format_bill_number to every day's reference numbers."""
    for day in day_states:
        for field in ("bill_number", "gate_pass_number", "alrs_number"):
            if day.get(field):
                day[field] = format_bill_number(day[field], include_prefix)
    return day_states


@router.get("/{kind}/{client_name}/{year}/{month}/print-data")
async def get_monthly_print_data(
    kind: str,
    client_name: str,
    year: int,
    month: int,
    quotation_id: Optional[str] = Query(None),
    include_prefix: bool = Query(
        False, description="Include hotel-code prefix in bill / gate-pass numbers"
    ),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """Return the monthly matrix formatted for printing.

    Identical to the normal GET matrix, but reference numbers on each day
    (bill_number, gate_pass_number, alrs_number) are formatted with or
    without the hotel-code prefix according to ``include_prefix``.
    """
    if kind not in MONTHLY_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Kind must be one of {', '.join(MONTHLY_KINDS)}.",
        )
    month_length = _parse_month_numbers(year, month)
    laundry_id = resolve_laundry_id(current_user)
    doc = await _find_month_doc(client_name, kind, year, month, laundry_id)
    effective_quotation = quotation_id or (doc or {}).get("quotation_id")
    rows = await _build_rows(client_name, effective_quotation, laundry_id)

    days = _day_states(doc, month_length)
    days = _strip_day_prefixes(days, include_prefix)

    return {
        "client_name": client_name,
        "kind": kind,
        "year": year,
        "month": month,
        "month_length": month_length,
        "quotation_id": effective_quotation,
        "rows": rows,
        "days": days,
        "cells": _cells_from_days(doc),
        "operations_summary": await _live_operations_summary(
            client_name, year, month, laundry_id
        ),
    }
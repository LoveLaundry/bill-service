"""Today Sheet — one-screen daily receiving, delivery and balance entry.

The mobile app already speaks this contract; the web sheet used to be a stub
because the API did not exist in this service. Both clients now share the same
two endpoints:

  GET  /today-sheet?date=YYYY-MM-DD
  POST /today-sheet/commit

Nothing here re-implements a balance. Gate passes are created through the
canonical ``create_gate_pass_record`` service and deliveries through the
canonical ``deliveries.create_delivery`` handler, so FIFO allocation, per-pass
validation, status derivation, events, audit and sync stay identical to the
ordinary Receive / Deliver screens.
"""
from datetime import date, datetime
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..app_time import day_query, parse_day, parse_wall_clock
from ..auth_helper import require_capability
from ..models import DeliveryCreate, DeliveryItem, GatePassCreate, GatePassItem
from ..services import balance_engine as be
from ..services import operations_context as ctx
from ..services.gate_pass_records import create_gate_pass_record, next_receiving_number
from ..tenancy import LOVE_LAUNDRY_ID, resolve_laundry_id
from .deliveries import create_delivery

router = APIRouter(tags=["today-sheet"])


# ── Wire models ────────────────────────────────────────────────────────────────

class TodayReceiveItem(BaseModel):
    item_name: str
    specification: Optional[str] = None
    unit: Optional[str] = None
    quantity: float = Field(gt=0)


class TodayDeliveryItem(BaseModel):
    item_name: str
    specification: Optional[str] = None
    quantity: float = Field(gt=0)
    """Quantity returning to the hotel.

    ``gate_pass_id`` omitted means AUTO: allocate from the oldest gate pass
    that still has this item available (FIFO across open passes).
    """
    gate_pass_id: Optional[str] = None


class TodayReceiveGroup(BaseModel):
    client_name: str
    items: List[TodayReceiveItem] = Field(min_length=1)
    received_by: Optional[str] = None


class TodayDeliveryGroup(BaseModel):
    client_name: str
    items: List[TodayDeliveryItem] = Field(min_length=1)
    received_by: Optional[str] = None


class TodaySheetCommit(BaseModel):
    date: str
    receives: List[TodayReceiveGroup] = Field(default_factory=list)
    deliveries: List[TodayDeliveryGroup] = Field(default_factory=list)


# ── Small helpers ──────────────────────────────────────────────────────────────

class _CommitRequest:
    """Bare request object for the canonical create handlers.

    ``create_delivery`` accepts a FastAPI ``Request`` only for the optional
    ``X-Idempotency-Key``. A sheet commit sends no per-line key, so an object
    with an empty header map is enough and keeps the shared handler untouched.
    """

    headers: Dict[str, str] = {}
    url = SimpleNamespace(path="/today-sheet/commit")


def _request() -> _CommitRequest:
    return _CommitRequest()


def _parse_date(value: str) -> date:
    try:
        return parse_day(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")


def _client(value: Optional[str]) -> str:
    return str(value or "").strip()


def _clean_spec(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _owned(doc: Optional[dict], laundry_id: str) -> bool:
    return bool(doc) and (doc.get("laundry_id") or LOVE_LAUNDRY_ID) == laundry_id


def _is_same_day(value: Optional[datetime], day: date) -> bool:
    if not value:
        return False
    try:
        return day_query(value).date() == day
    except (TypeError, ValueError):
        return False


def _error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("detail")
            if message:
                return str(message)
            errors = detail.get("errors")
            if isinstance(errors, list) and errors:
                return "; ".join(
                    str(err.get("detail") or err.get("message") or err)
                    if isinstance(err, dict) else str(err)
                    for err in errors
                )
        return str(detail)
    return str(exc) or exc.__class__.__name__


def _validate_delivery_qty(item_name: str, quantity: float) -> None:
    """Mirror the engine's curtain-only fractional rule with a clearer error."""
    if "curtain" not in item_name.casefold() and not float(quantity).is_integer():
        raise HTTPException(
            status_code=400,
            detail=f"Only curtain quantities may use fractional kilograms ({item_name}).",
        )


# ── GET: everything the operator needs for one day ────────────────────────────

@router.get("/today-sheet")
async def get_today_sheet(
    date: str = Query(..., description="Business day in YYYY-MM-DD (Sri Lanka)"),
    current_user: dict = Depends(require_capability("delivery:read")),
):
    """Return per-hotel pending stock and the movements recorded on ``date``.

    ``pending_passes`` uses the same ``build_availability`` rows as the Deliver
    screen, so the AUTO/FIFO quantity offered here is exactly what commit will
    accept. ``receives`` and ``deliveries`` are read-only history for the day.
    """
    day = _parse_date(date)
    laundry_id = resolve_laundry_id(current_user)

    gate_passes = [
        gp for gp in await ctx.load_gate_passes()
        if _owned(gp, laundry_id) and _client(gp.get("client_name"))
    ]
    gp_ids = [str(gp["gate_pass_id"]) for gp in gate_passes if gp.get("gate_pass_id")]
    deliveries, returns = await ctx.load_movements(gp_ids)

    rows = be.build_availability(
        gate_passes,
        ctx.delivered_by_gate_pass(deliveries),
        ctx.returned_by_gate_pass(returns),
    )

    clients: Dict[str, dict] = {}

    def entry_for(name: str) -> dict:
        key = _client(name)
        if key not in clients:
            clients[key] = {
                "client_name": key,
                "available_qty": 0.0,
                "pending_passes": [],
                "receives": [],
                "deliveries": [],
            }
        return clients[key]

    for row in rows:
        name = _client(row.get("client_name"))
        if not name or row.get("total_available_qty", 0) <= 0:
            continue
        entry = entry_for(name)
        entry["pending_passes"].append(row)
        entry["available_qty"] += be.as_qty(row.get("total_available_qty"))

    for gp in gate_passes:
        if not _is_same_day(gp.get("receiving_date"), day):
            continue
        entry = entry_for(gp.get("client_name"))
        lines = []
        for item in gp.get("items") or []:
            lines.append({
                "item_name": str(item.get("item_name") or ""),
                "specification": item.get("specification") or "",
                "unit": item.get("unit") or None,
                "quantity": be.as_qty(item.get("received_qty")),
            })
        entry["receives"].append({
            "gate_pass_id": str(gp.get("gate_pass_id") or ""),
            "gate_pass_number": str(gp.get("gate_pass_number") or ""),
            "received_by": str(gp.get("received_by") or ""),
            "items": lines,
        })

    for delivery in deliveries:
        if delivery.get("status") in (be.CANCELLED_STATUS, be.DRAFT_STATUS):
            continue
        if not _is_same_day(delivery.get("delivery_date"), day):
            continue
        entry = entry_for(delivery.get("client_name"))
        lines = []
        for item in delivery.get("items") or []:
            lines.append({
                "item_name": str(item.get("item_name") or ""),
                "specification": item.get("specification") or "",
                "quantity": be.as_qty(item.get("quantity")),
                "gate_pass_id": str(
                    item.get("gate_pass_id") or delivery.get("gate_pass_id") or ""
                ),
            })
        entry["deliveries"].append({
            "delivery_id": str(delivery.get("id") or ""),
            "received_by": str(delivery.get("received_by") or ""),
            "items": lines,
        })

    client_list = sorted(clients.values(), key=lambda c: c["client_name"].casefold())
    for client in client_list:
        client["pending_passes"].sort(
            key=lambda p: (
                str(p.get("receiving_date") or ""),
                str(p.get("gate_pass_number") or ""),
            )
        )
        client["available_qty"] = round(client["available_qty"], 4)

    return {
        "date": day.isoformat(),
        "clients": client_list,
        "totals": {
            "clients": len(client_list),
            "pending_passes": sum(len(c["pending_passes"]) for c in client_list),
            "available_qty": round(sum(c["available_qty"] for c in client_list), 4),
            "received_lines": sum(
                len(r["items"]) for c in client_list for r in c["receives"]
            ),
            "received_qty": round(
                sum(
                    be.as_qty(i.get("quantity"))
                    for c in client_list
                    for r in c["receives"]
                    for i in r["items"]
                ), 4
            ),
            "delivery_lines": sum(
                len(d["items"]) for c in client_list for d in c["deliveries"]
            ),
            "delivered_qty": round(
                sum(
                    be.as_qty(i.get("quantity"))
                    for c in client_list
                    for d in c["deliveries"]
                    for i in d["items"]
                ), 4
            ),
        },
    }


# ── POST: commit receives and deliveries ──────────────────────────────────────

async def _commit_receive(
    group: TodayReceiveGroup,
    day: date,
    current_user: dict,
    laundry_id: str,
) -> dict:
    client = _client(group.client_name)
    if not client:
        raise HTTPException(status_code=400, detail="Client name is required")
    if not group.items:
        raise HTTPException(status_code=400, detail="At least one received item is required")

    receiving_date = parse_wall_clock(day.isoformat())
    gate_pass_number = await next_receiving_number(receiving_date)
    payload = GatePassCreate(
        gate_pass_number=gate_pass_number,
        client_name=client,
        receiving_date=receiving_date,
        received_by=_client(group.received_by) or current_user.get("user_name") or "Today Sheet",
        items=[
            GatePassItem(
                item_name=str(item.item_name).strip(),
                specification=_clean_spec(item.specification),
                unit=_clean_spec(item.unit),
                client_qty=item.quantity,
                received_qty=item.quantity,
            )
            for item in group.items
        ],
        notes="Recorded from Today Sheet",
        origin={"kind": "today_sheet", "date": day.isoformat()},
        status="RECEIVED",
    )
    created = await create_gate_pass_record(
        payload,
        current_user,
        status="RECEIVED",
        origin=payload.origin,
        laundry_id=laundry_id,
    )
    return created


async def _allocate_delivery_lines(
    group: TodayDeliveryGroup,
    day: date,
    current_user: dict,
    laundry_id: str,
) -> List[dict]:
    """Return one canonical delivery per source pass for this hotel group.

    Explicit source passes are reserved first; AUTO lines then consume what is
    left, oldest pass first. Lines targeting the same (pass, item) are merged,
    because the canonical planner deliberately rejects duplicate buckets.
    """
    client = _client(group.client_name)
    if not client:
        raise HTTPException(status_code=400, detail="Client name is required")

    gate_passes = [
        gp for gp in await ctx.load_gate_passes()
        if _owned(gp, laundry_id) and be.same_client(gp.get("client_name"), client)
    ]
    if not gate_passes:
        raise HTTPException(
            status_code=404,
            detail=f"No active gate passes found for {client}. Receive the linen first.",
        )
    gate_passes.sort(
        key=lambda gp: (
            str(gp.get("receiving_date") or ""),
            str(gp.get("created_at") or ""),
            str(gp.get("gate_pass_id") or ""),
        )
    )

    gp_ids = [str(gp["gate_pass_id"]) for gp in gate_passes if gp.get("gate_pass_id")]
    deliveries, returns = await ctx.load_movements(gp_ids)
    available = ctx.availability_maps(
        gate_passes,
        ctx.delivered_by_gate_pass(deliveries),
        ctx.returned_by_gate_pass(returns),
    )
    available = {gp_id: dict(bucket) for gp_id, bucket in available.items()}

    # (gate_pass_id, item_key) -> {qty, item_name, specification}
    buckets: Dict[Tuple[str, str], dict] = {}

    def add_bucket(gp_id: str, line: TodayDeliveryItem, quantity: float) -> None:
        name = str(line.item_name).strip()
        spec = _clean_spec(line.specification)
        key = be.item_key(name, spec)
        bucket = buckets.setdefault(
            (gp_id, key),
            {"item_name": name, "specification": spec, "quantity": 0.0},
        )
        bucket["quantity"] += quantity

    # Explicit passes win: honour the operator's choice before AUTO fills.
    for line in group.items:
        name = str(line.item_name).strip()
        if not name:
            raise HTTPException(status_code=400, detail="Item name is required")
        _validate_delivery_qty(name, line.quantity)
        if not line.gate_pass_id:
            continue
        gp_id = str(line.gate_pass_id).strip()
        if gp_id not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Source gate pass {gp_id} was not found for {client}.",
            )
        key = be.item_key(name, _clean_spec(line.specification))
        if be.as_qty(available[gp_id].get(key)) < line.quantity:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{name}: not enough available on gate pass {gp_id} "
                    f"({be.as_qty(available[gp_id].get(key))} left)."
                ),
            )
        available[gp_id][key] = be.as_qty(available[gp_id].get(key)) - line.quantity
        add_bucket(gp_id, line, line.quantity)

    # AUTO: oldest pass with stock first, spilling into the next pass when a
    # quantity spans more than one batch.
    for line in group.items:
        if line.gate_pass_id:
            continue
        name = str(line.item_name).strip()
        key = be.item_key(name, _clean_spec(line.specification))
        remaining = float(line.quantity)
        for gp_id in [str(gp["gate_pass_id"]) for gp in gate_passes if gp.get("gate_pass_id")]:
            left = be.as_qty(available[gp_id].get(key))
            if left <= 0:
                continue
            take = min(left, remaining)
            available[gp_id][key] = left - take
            add_bucket(gp_id, line, take)
            remaining -= take
            if remaining <= 1e-6:
                break
        if remaining > 1e-6:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{name}: only {float(line.quantity) - remaining:g} of "
                    f"{line.quantity:g} is available for {client}."
                ),
            )

    if not buckets:
        raise HTTPException(status_code=400, detail="No deliverable lines were entered")

    by_pass: Dict[str, List[DeliveryItem]] = {}
    for (gp_id, _key), bucket in buckets.items():
        by_pass.setdefault(gp_id, []).append(
            DeliveryItem(
                item_name=bucket["item_name"],
                specification=bucket["specification"],
                quantity=round(bucket["quantity"], 4),
                gate_pass_id=gp_id,
            )
        )

    delivery_date = parse_wall_clock(day.isoformat())
    actor = current_user.get("user_name") or "Today Sheet"
    created: List[dict] = []
    for gp_id, lines in by_pass.items():
        payload = DeliveryCreate(
            gate_pass_id=gp_id,
            client_name=client,
            delivery_date=delivery_date,
            delivered_by=actor,
            received_by=_client(group.received_by) or actor,
            items=lines,
            notes="Recorded from Today Sheet",
            status="DELIVERED",
            origin={"kind": "today_sheet", "date": day.isoformat()},
        )
        created.append(await create_delivery(payload, current_user, request=_request()))
    return created


@router.post("/today-sheet/commit")
async def commit_today_sheet(
    payload: TodaySheetCommit,
    current_user: dict = Depends(require_capability("gatepass:write")),
):
    """Best-effort commit: one result per group, so one bad row never hides the rest.

    This mirrors the mobile flow exactly — receives are created first, then
    deliveries are allocated against the refreshed balance. The management
    service posts the expense lines separately; this endpoint only owns linen.
    """
    day = _parse_date(payload.date)
    laundry_id = resolve_laundry_id(current_user)
    results: List[dict] = []

    for group in payload.receives:
        client = _client(group.client_name)
        try:
            created = await _commit_receive(group, day, current_user, laundry_id)
            results.append({
                "kind": "receive",
                "client_name": client,
                "ok": True,
                "id": created.get("id"),
                "gate_pass_number": created.get("gate_pass_number"),
                "quantity": round(sum(item.quantity for item in group.items), 4),
            })
        except Exception as exc:  # one hotel's receiving must not block the rest
            results.append({
                "kind": "receive",
                "client_name": client,
                "ok": False,
                "error": _error_text(exc),
            })

    for group in payload.deliveries:
        client = _client(group.client_name)
        try:
            created = await _allocate_delivery_lines(group, day, current_user, laundry_id)
            results.append({
                "kind": "delivery",
                "client_name": client,
                "ok": True,
                "ids": [row.get("id") for row in created],
                "quantity": round(sum(item.quantity for item in group.items), 4),
            })
        except Exception as exc:
            results.append({
                "kind": "delivery",
                "client_name": client,
                "ok": False,
                "error": _error_text(exc),
            })

    succeeded = sum(1 for row in results if row["ok"])
    return {
        "date": day.isoformat(),
        "results": results,
        "summary": {
            "groups": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "received_qty": round(
                sum(
                    be.as_qty(row.get("quantity"))
                    for row in results
                    if row["kind"] == "receive" and row["ok"]
                ), 4
            ),
            "delivered_qty": round(
                sum(
                    be.as_qty(row.get("quantity"))
                    for row in results
                    if row["kind"] == "delivery" and row["ok"]
                ), 4
            ),
        },
    }

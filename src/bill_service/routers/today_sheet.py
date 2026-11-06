"""Today sheet — one screen for the whole business day's stock entry.

``GET /today-sheet`` aggregates everything an operator needs for a day: which
shops received stock, which delivered, and the deliverable inventory per open
gate pass. ``POST /today-sheet/commit`` turns the sheet into real gate passes
and deliveries through the exact same code paths as the individual create
endpoints — balances, journals, sync and monthly back-sync are unchanged.

Deliveries may name a source ``gate_pass_id`` per line; lines without one are
assigned FIFO (oldest receiving pass with availability first), splitting a
line across passes when one pass cannot cover the quantity.
"""
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..app_time import day_bounds, parse_day, parse_wall_clock, today
from ..auth_helper import ROLE_CAPABILITIES, get_current_user
from ..models import (
    DeliveryCreate,
    DeliveryItem,
    GatePassCreate,
    GatePassItem,
    TodaySheetCommit,
)
from ..services import balance_engine as be
from ..services import operations_context as ctx
from ..services.gate_pass_records import create_gate_pass_record, next_receiving_number
from ..services.monthly_sync import sync_gate_pass_to_monthly
from ..router_utils import log_audit
from ..tenancy import resolve_laundry_id
from .deliveries import create_delivery

router = APIRouter(prefix="/today-sheet", tags=["today-sheet"])

MONTH_SENSITIVE_FIELDS = ["client_name", "notes"]


class _CommitRequest:
    """Minimal Request stand-in for the shared create endpoints.

    The per-entity creates read ``X-Idempotency-Key`` and ``url.path`` off the
    request; a sheet commit has no per-row key, so an empty header map makes
    every idempotency lookup a no-op while keeping the call signature intact.
    """

    headers: Dict[str, str] = {}

    class url:  # noqa: D106 - mirrors Starlette's request.url.path
        path = "/today-sheet/commit"


def _has_capability(user: dict, capability: str) -> bool:
    return capability in ROLE_CAPABILITIES.get(user.get("role", ""), [])


def _parse_instant(value: Any) -> Optional[datetime]:
    """Best-effort datetime for a stored receiving/delivery date."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _in_day(value: Any, start: datetime, end: datetime) -> bool:
    instant = _parse_instant(value)
    if instant is None:
        return False
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return start <= instant < end


async def _monthly_clients(year: int, month: int, laundry_id: str) -> List[str]:
    """Client names on this month's operations matrix (planned work counts)."""
    from ..crypto_helper import decrypt_dict
    from ..database.main_db import monthly_entries_collection

    names: List[str] = []
    cursor = monthly_entries_collection.find(
        {"year": year, "month": month, "laundry_id": laundry_id}
    )
    async for doc in cursor:
        try:
            name = (decrypt_dict(doc, MONTH_SENSITIVE_FIELDS).get("client_name") or "").strip()
        except Exception:
            continue
        if name:
            names.append(name)
    return names


@router.get("")
async def today_sheet(
    date: str = Query(..., description="Business day YYYY-MM-DD (LKT)"),
    current_user: dict = Depends(get_current_user),
):
    """Everything the daily sheet needs for one business day, in one payload."""
    try:
        day = parse_day(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    start, end = day_bounds(day)
    laundry_id = resolve_laundry_id(current_user)

    gate_passes = [
        gp
        for gp in await ctx.load_gate_passes()
        if (gp.get("laundry_id") or "") in ("", laundry_id)
    ]
    gp_ids = [str(gp["gate_pass_id"]) for gp in gate_passes]
    deliveries, returns = await ctx.load_movements(gp_ids)
    delivered_map = ctx.delivered_by_gate_pass(deliveries)
    returned_map = ctx.returned_by_gate_pass(returns)
    availability = be.build_availability(gate_passes, delivered_map, returned_map)

    receives_today = [
        {
            "id": str(gp.get("id") or gp.get("gate_pass_id")),
            "gate_pass_number": gp.get("gate_pass_number"),
            "receiving_date": gp.get("receiving_date"),
            "items": [
                {
                    "item_name": i.get("item_name"),
                    "specification": i.get("specification"),
                    "received_qty": i.get("received_qty"),
                    "unit": i.get("unit"),
                }
                for i in (gp.get("items") or [])
            ],
        }
        for gp in gate_passes
        if _in_day(gp.get("receiving_date"), start, end)
    ]

    deliveries_today = [
        {
            "id": str(d.get("id")),
            "delivery_date": d.get("delivery_date"),
            "status": d.get("status"),
            "items": [
                {
                    "item_name": i.get("item_name"),
                    "specification": i.get("specification"),
                    "quantity": i.get("quantity"),
                    "gate_pass_id": i.get("gate_pass_id"),
                }
                for i in (d.get("items") or [])
            ],
        }
        for d in deliveries
        if d.get("status") != "CANCELLED" and _in_day(d.get("delivery_date"), start, end)
    ]

    # One row per shop: union of gate-pass clients, today's delivery clients and
    # this month's planned-operations clients.
    client_names: Dict[str, str] = {}

    def _add(name: Any):
        cleaned = (name or "").strip()
        if cleaned:
            client_names.setdefault(be.normalize_client(cleaned), cleaned)

    for gp in gate_passes:
        _add(gp.get("client_name"))
    for d in deliveries_today:
        _add(d.get("client_name"))
    for name in await _monthly_clients(day.year, day.month, laundry_id):
        _add(name)

    clients = []
    pass_by_id = {str(gp.get("id") or gp.get("gate_pass_id")): gp for gp in gate_passes}
    delivery_by_id = {str(d.get("id")): d for d in deliveries}
    for canonical, display in sorted(client_names.items()):
        rows = [r for r in availability if be.same_client(r.get("client_name"), display)]
        receives = []
        for r in receives_today:
            gp = pass_by_id.get(r["id"])
            if gp and be.same_client(gp.get("client_name"), display):
                receives.append(r)
        dels = [
            d for d in deliveries_today
            if be.same_client((delivery_by_id.get(d["id"]) or {}).get("client_name"), display)
        ]
        clients.append(
            {
                "client_name": display,
                "receives": receives,
                "deliveries": dels,
                "pending_passes": rows,
                "received_qty": sum(
                    float(i.get("received_qty") or 0)
                    for r in receives
                    for i in r["items"]
                ),
                "delivered_qty": sum(
                    float(i.get("quantity") or 0) for d in dels for i in d["items"]
                ),
                "available_qty": sum(r["total_available_qty"] for r in rows),
            }
        )

    return {"date": date, "clients": clients}


async def _auto_assign_lines(
    client_name: str,
    lines: List[Dict[str, Any]],
    laundry_id: str,
) -> List[DeliveryItem]:
    """Split delivery lines across a client's open passes, oldest first (FIFO).

    Lines that already name a ``gate_pass_id`` pass through untouched. For the
    rest, the earliest pass with availability for that item absorbs as much as
    it can and the remainder flows to the next pass. Raises HTTPException 400
    when any quantity cannot be covered.
    """
    gate_passes = [
        gp
        for gp in await ctx.load_gate_passes()
        if be.same_client(gp.get("client_name"), client_name)
        and (gp.get("laundry_id") or "") in ("", laundry_id)
    ]
    gp_ids = [str(gp["gate_pass_id"]) for gp in gate_passes]
    deliveries, returns = await ctx.load_movements(gp_ids)
    rows = be.build_availability(
        gate_passes,
        ctx.delivered_by_gate_pass(deliveries),
        ctx.returned_by_gate_pass(returns),
    )
    rows.sort(key=lambda r: _parse_instant(r.get("receiving_date")) or datetime.min.replace(tzinfo=timezone.utc))

    # remaining[(pass_id, item_key)] = qty still assignable
    remaining: Dict[tuple, float] = {}
    for row in rows:
        for item in row.get("deliverable_items", []):
            key = (row["gate_pass_id"], be.item_key(item["item_name"], item.get("specification")))
            remaining[key] = float(item.get("available_qty") or 0)

    planned: List[DeliveryItem] = []
    unassigned: List[str] = []
    for line in lines:
        qty = float(line["quantity"])
        explicit = line.get("gate_pass_id")
        if explicit:
            planned.append(
                DeliveryItem(
                    item_name=line["item_name"],
                    specification=line.get("specification"),
                    gate_pass_id=explicit,
                    quantity=qty,
                )
            )
            continue
        key_name = be.item_key(line["item_name"], line.get("specification"))
        left = qty
        for row in rows:
            if left <= 0:
                break
            pool = remaining.get((row["gate_pass_id"], key_name), 0)
            if pool <= 0:
                continue
            take = min(left, pool)
            remaining[(row["gate_pass_id"], key_name)] = pool - take
            planned.append(
                DeliveryItem(
                    item_name=line["item_name"],
                    specification=line.get("specification"),
                    gate_pass_id=row["gate_pass_id"],
                    quantity=take,
                )
            )
            left -= take
        if left > 0:
            unassigned.append(f"{line['item_name']} ({left:g} short)")

    if unassigned:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Not enough received stock for {client_name}: "
                + ", ".join(unassigned)
            ),
        )
    return planned


@router.post("/commit")
async def commit_today_sheet(
    payload: TodaySheetCommit,
    current_user: dict = Depends(get_current_user),
):
    """Create the day's gate passes and deliveries in one call.

    Receives commit first so same-day stock is deliverable immediately after.
    Each entry is independent: one failure is reported per row and never
    silently rolls back the rest of the sheet.
    """
    if not payload.receives and not payload.deliveries:
        raise HTTPException(status_code=400, detail="Nothing to commit")
    try:
        day = parse_day(payload.date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    if day > today():
        raise HTTPException(status_code=409, detail="Cannot enter a future day")

    if payload.receives and not _has_capability(current_user, "gatepass:write"):
        raise HTTPException(status_code=403, detail="Missing gatepass:write")
    if payload.deliveries and not _has_capability(current_user, "delivery:write"):
        raise HTTPException(status_code=403, detail="Missing delivery:write")

    laundry_id = resolve_laundry_id(current_user)
    operator = current_user.get("user_name") or current_user.get("auth_id", "system")
    day_start = parse_wall_clock(payload.date)
    origin = {"kind": "today_sheet", "date": payload.date}
    results: List[Dict[str, Any]] = []

    # ── Receives first: today's stock must exist before it can be delivered ──
    for receive in payload.receives:
        client = (receive.client_name or "").strip()
        if not client:
            results.append({"section": "receive", "client_name": receive.client_name, "ok": False, "error": "client_name is required"})
            continue
        try:
            items = [
                GatePassItem(
                    item_name=i.item_name,
                    specification=i.specification,
                    client_qty=i.quantity,
                    received_qty=i.quantity,
                    unit=i.unit,
                    piece_count=i.piece_count,
                )
                for i in receive.items
            ]
            number = await next_receiving_number(day_start)
            # Server-generated number + shared record builder: identical to the
            # monthly DRAFT path, so journal/sync/verify stay consistent. The
            # route-level reserve_references step is skipped on purpose — the
            # number was already claimed by next_receiving_number.
            gp = await create_gate_pass_record(
                GatePassCreate(
                    gate_pass_number=number,
                    client_name=client,
                    receiving_date=day_start,
                    received_by=operator,
                    items=items,
                    notes=receive.notes,
                    status="RECEIVED",
                    origin=origin,
                ),
                current_user,
                status="RECEIVED",
                origin=origin,
                laundry_id=laundry_id,
            )
            try:
                await sync_gate_pass_to_monthly(gp, actor_id=current_user.get("auth_id", "system"))
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "sync_gate_pass_to_monthly failed for gate pass %s", gp.get("id")
                )
            await log_audit(
                current_user.get("auth_id", "system"),
                "RECEIVING_CREATE",
                "gatepass",
                gp["id"],
                laundry_id=laundry_id,
            )
            results.append(
                {
                    "section": "receive",
                    "client_name": client,
                    "ok": True,
                    "id": gp.get("id"),
                    "gate_pass_number": gp.get("gate_pass_number"),
                }
            )
        except HTTPException as exc:
            results.append({"section": "receive", "client_name": client, "ok": False, "error": exc.detail})
        except Exception as exc:  # never let one row kill the sheet
            results.append({"section": "receive", "client_name": client, "ok": False, "error": str(exc)})

    # ── Deliveries: explicit pass ids pass through; the rest go FIFO ──
    for delivery in payload.deliveries:
        client = (delivery.client_name or "").strip()
        if not client:
            results.append({"section": "delivery", "client_name": delivery.client_name, "ok": False, "error": "client_name is required"})
            continue
        try:
            raw_lines = [line.model_dump() for line in delivery.items]
            items = await _auto_assign_lines(client, raw_lines, laundry_id)
            record = await create_delivery(
                DeliveryCreate(
                    client_name=client,
                    delivery_date=day_start,
                    delivered_by=operator,
                    received_by=(delivery.received_by or "").strip() or operator,
                    items=items,
                    notes=delivery.notes,
                    status="DELIVERED",
                    origin=origin,
                ),
                current_user,
                _CommitRequest(),
            )
            results.append(
                {
                    "section": "delivery",
                    "client_name": client,
                    "ok": True,
                    "id": record.get("id"),
                }
            )
        except HTTPException as exc:
            results.append({"section": "delivery", "client_name": client, "ok": False, "error": exc.detail})
        except Exception as exc:
            results.append({"section": "delivery", "client_name": client, "ok": False, "error": str(exc)})

    return {"date": payload.date, "results": results}

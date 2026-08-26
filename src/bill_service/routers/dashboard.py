from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..crypto_helper import decrypt_dict
from ..database.main_db import (
    bills_collection,
    gatepasses_collection,
    deliveries_collection,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

BILL_DECRYPT_FIELDS = ["client_name"]
GP_DECRYPT_FIELDS = ["client_name", "items"]
DELIVERY_DECRYPT_FIELDS = ["items"]

PERIOD_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "quarter": 90,
    "year": 365,
}


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _windows(period: str):
    now = datetime.now(timezone.utc)
    span = PERIOD_DAYS.get(period, 30)
    cur_start = now - timedelta(days=span)
    prev_end = cur_start - timedelta(microseconds=1)
    prev_start = prev_end - timedelta(days=span)
    return cur_start, now, prev_start, prev_end


async def _fetch_bills(start: datetime, end: datetime):
    docs = []
    cursor = bills_collection.find(
        {"created_at": {"$gte": start, "$lte": end}},
        {
            "client_name": 1,
            "grand_total": 1,
            "paid_amount": 1,
            "outstanding_amount": 1,
            "payment_status": 1,
            "created_at": 1,
            "_id": 1,
        },
    )
    async for d in cursor:
        try:
            dec = decrypt_dict(d, BILL_DECRYPT_FIELDS)
            client = dec.get("client_name") or "Unknown"
        except Exception:
            client = "Unknown"
        docs.append(
            {
                "client_name": client,
                "grand_total": float(d.get("grand_total") or 0),
                "paid_amount": float(d.get("paid_amount") or 0),
                "outstanding_amount": float(d.get("outstanding_amount") or 0),
                "payment_status": (d.get("payment_status") or "DRAFT"),
                "created_at": _utc(d.get("created_at")),
            }
        )
    return docs


async def _fetch_gate_passes(start: datetime, end: datetime):
    out = []
    cursor = gatepasses_collection.find(
        {"created_at": {"$gte": start, "$lte": end}},
        {"client_name": 1, "items": 1, "gate_pass_number": 1, "created_at": 1, "_id": 1},
    )
    async for d in cursor:
        try:
            dec = decrypt_dict(d, GP_DECRYPT_FIELDS)
            client = dec.get("client_name") or "Unknown"
            items = dec.get("items") or []
        except Exception:
            client = "Unknown"
            items = []
        out.append(
            {
                "id": str(d.get("_id")),
                "gate_pass_number": d.get("gate_pass_number"),
                "client_name": client,
                "created_at": _utc(d.get("created_at")),
                "items": [
                    {
                        "item_name": it.get("item_name"),
                        "received_qty": int(it.get("received_qty") or 0),
                    }
                    for it in items
                ],
            }
        )
    return out


async def _fetch_deliveries(gate_pass_ids: List[str]):
    if not gate_pass_ids:
        return []
    out = []
    cursor = deliveries_collection.find(
        {"gate_pass_id": {"$in": gate_pass_ids}},
        {"gate_pass_id": 1, "items": 1, "created_at": 1, "_id": 1},
    )
    async for d in cursor:
        try:
            dec = decrypt_dict(d, DELIVERY_DECRYPT_FIELDS)
            items = dec.get("items") or []
        except Exception:
            items = []
        out.append(
            {
                "gate_pass_id": d.get("gate_pass_id"),
                "items": [
                    {
                        "item_name": it.get("item_name"),
                        "quantity": int(it.get("quantity") or 0),
                    }
                    for it in items
                ],
            }
        )
    return out


def _bucket_key(dt: Optional[datetime], period: str):
    if dt is None:
        return None
    if period == "day":
        h = f"{dt.hour:02d}:00"
        return (h, h)
    if period in ("quarter", "year"):
        key = f"{dt.year}-{dt.month:02d}"
        return (key, key)
    key = dt.date().isoformat()
    return (key, f"{dt.month}/{dt.day}")


def _outstanding(b):
    if b["outstanding_amount"]:
        return b["outstanding_amount"]
    return max(0.0, b["grand_total"] - b["paid_amount"])


def aggregate(bills, gate_passes, deliveries, period):
    revenue = sum(b["grand_total"] for b in bills)
    collected = sum(b["paid_amount"] for b in bills)
    outstanding = sum(_outstanding(b) for b in bills)
    bill_count = len(bills)
    paid = sum(1 for b in bills if b["payment_status"] == "PAID")
    partial = sum(1 for b in bills if b["payment_status"] == "PARTIALLY_PAID")
    pending = sum(
        1
        for b in bills
        if b["payment_status"] not in ("PAID", "PARTIALLY_PAID", "CANCELLED")
    )
    collection_rate = (collected / revenue * 100) if revenue else 0.0
    avg = (revenue / bill_count) if bill_count else 0.0

    gp_count = len(gate_passes)
    items_received = sum(it["received_qty"] for gp in gate_passes for it in gp["items"])

    del_map = {}
    for d in deliveries:
        m = del_map.setdefault(d["gate_pass_id"], {})
        for it in d["items"]:
            m[it["item_name"]] = m.get(it["item_name"], 0) + it["quantity"]
    items_delivered = sum(it["quantity"] for d in deliveries for it in d["items"])
    items_pending = max(0, items_received - items_delivered)

    active_clients = len(set(b["client_name"] for b in bills))

    client_agg = {}
    for b in bills:
        c = client_agg.setdefault(
            b["client_name"], {"revenue": 0.0, "outstanding": 0.0, "bills": 0}
        )
        c["revenue"] += b["grand_total"]
        c["outstanding"] += _outstanding(b)
        c["bills"] += 1
    top_clients = sorted(
        ({"client_name": k, **v} for k, v in client_agg.items()),
        key=lambda x: -x["revenue"],
    )[:6]

    series = {}
    for b in bills:
        k = _bucket_key(b["created_at"], period)
        if not k:
            continue
        key, label = k
        e = series.setdefault(key, {"label": label, "revenue": 0.0, "collected": 0.0})
        e["revenue"] += b["grand_total"]
        e["collected"] += b["paid_amount"]
    revenue_series = sorted(
        ({"key": k, **v} for k, v in series.items()), key=lambda x: x["key"]
    )

    st = {"PAID": 0.0, "PARTIAL": 0.0, "PENDING": 0.0}
    for b in bills:
        amt = b["grand_total"]
        if b["payment_status"] == "PAID":
            st["PAID"] += amt
        elif b["payment_status"] == "PARTIALLY_PAID":
            st["PARTIAL"] += amt
        elif b["payment_status"] != "CANCELLED":
            st["PENDING"] += amt
    payment_status = [
        {"name": "Paid", "value": round(st["PAID"], 2), "color": "#16A34A"},
        {"name": "Partial", "value": round(st["PARTIAL"], 2), "color": "#F59E0B"},
        {"name": "Unpaid", "value": round(st["PENDING"], 2), "color": "#DC2626"},
    ]
    payment_status = [s for s in payment_status if s["value"] > 0]

    pending_gps = []
    for gp in gate_passes:
        dm = del_map.get(gp["id"], {})
        pending_items = sum(
            max(0, it["received_qty"] - dm.get(it["item_name"], 0)) for it in gp["items"]
        )
        if pending_items > 0:
            pending_gps.append(
                {
                    "gate_pass_number": gp["gate_pass_number"],
                    "client_name": gp["client_name"],
                    "pending": pending_items,
                }
            )
    pending_gps.sort(key=lambda x: -x["pending"])
    pending_gps = pending_gps[:6]

    return {
        "revenue": round(revenue, 2),
        "collected": round(collected, 2),
        "outstanding": round(outstanding, 2),
        "collectionRate": round(collection_rate, 2),
        "avgBill": round(avg, 2),
        "billCount": bill_count,
        "paidBills": paid,
        "partialBills": partial,
        "pendingBills": pending,
        "gatePasses": gp_count,
        "itemsReceived": items_received,
        "itemsDelivered": items_delivered,
        "itemsPending": items_pending,
        "activeClients": active_clients,
        "topClients": top_clients,
        "revenueSeries": revenue_series,
        "paymentStatus": payment_status,
        "pendingGatePasses": pending_gps,
    }


@router.get("/summary")
async def get_dashboard_summary(
    period: str = Query("month", pattern="^(day|week|month|quarter|year)$"),
    current_user: dict = Depends(require_capability("dashboard:read")),
):
    cur_start, cur_end, prev_start, prev_end = _windows(period)

    cur_bills = await _fetch_bills(cur_start, cur_end)
    prev_bills = await _fetch_bills(prev_start, prev_end)
    cur_gps = await _fetch_gate_passes(cur_start, cur_end)
    cur_dels = await _fetch_deliveries([gp["id"] for gp in cur_gps])
    prev_gps = await _fetch_gate_passes(prev_start, prev_end)
    prev_dels = await _fetch_deliveries([gp["id"] for gp in prev_gps])

    current = aggregate(cur_bills, cur_gps, cur_dels, period)
    previous = aggregate(prev_bills, prev_gps, prev_dels, period)

    cur_clients = {b["client_name"] for b in cur_bills}
    prev_clients = {b["client_name"] for b in prev_bills}
    current["newClients"] = len(cur_clients - prev_clients)
    previous["newClients"] = 0

    for m in (current, previous):
        m["quotationsDraft"] = 0
        m["quotationsSent"] = 0
        m["quotationsAccepted"] = 0
        m["quotationAcceptedValue"] = 0.0

    return {"current": current, "previous": previous}

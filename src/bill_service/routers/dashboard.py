from datetime import datetime, timezone
from typing import Dict, List, Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, get_search_token
from ..database import (
    bills_collection,
    deliveries_collection,
    gatepasses_collection,
    payments_collection,
    audit_collection,
)

router = APIRouter(tags=["dashboard"])

SENSITIVE_FIELDS_GP = ["client_name", "items", "notes"]
SENSITIVE_FIELDS_DEL = ["client_name", "items", "notes"]
SENSITIVE_FIELDS_BILL = ["client_name", "quotation_title", "notes", "items"]
SENSITIVE_FIELDS_PAY = ["client_name", "notes"]


def _decrypt_gp(doc: dict) -> dict:
    dec = decrypt_dict(doc, SENSITIVE_FIELDS_GP)
    dec["id"] = str(dec["_id"])
    del dec["_id"]
    return dec


def _decrypt_del(doc: dict) -> dict:
    dec = decrypt_dict(doc, SENSITIVE_FIELDS_DEL)
    dec["id"] = str(dec["_id"])
    del dec["_id"]
    return dec


def _decrypt_bill(doc: dict) -> dict:
    dec = decrypt_dict(doc, SENSITIVE_FIELDS_BILL)
    dec["id"] = str(dec["_id"])
    del dec["_id"]
    return dec


def _decrypt_pay(doc: dict) -> dict:
    dec = decrypt_dict(doc, SENSITIVE_FIELDS_PAY)
    dec["id"] = str(dec["_id"])
    del dec["_id"]
    return dec


# --- 1. Client Dashboard Summary ---
@router.get(
    "/dashboard/client-summary",
    dependencies=[Depends(require_capability("dashboard:read"))],
)
async def get_client_summary(client_name: str = Query(...)):
    token = get_search_token(client_name)

    gp_cursor = gatepasses_collection.find({"client_name_search": token})
    del_cursor = deliveries_collection.find({"client_name_search": token})
    bill_cursor = bills_collection.find({"client_name_search": token})
    pay_cursor = payments_collection.find({"client_name_search": token})

    gps = []
    async for d in gp_cursor:
        try:
            gps.append(_decrypt_gp(d))
        except Exception:
            pass

    deliveries = []
    async for d in del_cursor:
        try:
            deliveries.append(_decrypt_del(d))
        except Exception:
            pass

    bills = []
    async for d in bill_cursor:
        try:
            bills.append(_decrypt_bill(d))
        except Exception:
            pass

    payments = []
    async for d in pay_cursor:
        try:
            payments.append(_decrypt_pay(d))
        except Exception:
            pass

    # Compute stats
    total_received = 0
    open_mismatches = 0
    mismatches_list = []
    balances_map: Dict[str, dict] = {}

    for gp in gps:
        for item in gp.get("items", []):
            name = item["item_name"]
            client_qty = item.get("client_qty", 0)
            rec_qty = item.get("received_qty", 0)
            diff = item.get("difference", 0)
            total_received += rec_qty

            if diff != 0:
                open_mismatches += 1
                mismatches_list.append(
                    {
                        "gate_pass_id": gp["id"],
                        "gate_pass_number": gp["gate_pass_number"],
                        "item_name": name,
                        "expected": client_qty,
                        "received": rec_qty,
                        "difference": diff,
                        "reason": item.get("mismatch_reason", "OTHER"),
                        "notes": item.get("mismatch_notes"),
                        "date": gp.get("receiving_date"),
                    }
                )

            if name not in balances_map:
                balances_map[name] = {"received": 0, "delivered": 0, "pending": 0}
            balances_map[name]["received"] += rec_qty

    total_delivered = 0
    for dl in deliveries:
        if dl.get("status") == "CANCELLED":
            continue
        for item in dl.get("items", []):
            name = item["item_name"]
            qty = item["quantity"]
            total_delivered += qty
            if name not in balances_map:
                balances_map[name] = {"received": 0, "delivered": 0, "pending": 0}
            balances_map[name]["delivered"] += qty

    pending_items_sum = 0
    for name, item_bal in balances_map.items():
        item_bal["pending"] = max(0, item_bal["received"] - item_bal["delivered"])
        pending_items_sum += item_bal["pending"]

    total_billed = 0.0
    total_paid = 0.0
    outstanding_amt = 0.0
    pending_bills_count = 0
    for bill in bills:
        if bill.get("payment_status") == "CANCELLED":
            continue
        total_billed += bill.get("grand_total", 0.0)
        total_paid += bill.get("paid_amount", 0.0)
        outstanding_amt += bill.get("outstanding_amount", 0.0)
        if bill.get("payment_status") not in ["PAID"]:
            pending_bills_count += 1

    return {
        "stats": {
            "total_gate_passes": len(gps),
            "total_items_received": total_received,
            "total_items_delivered": total_delivered,
            "pending_items": pending_items_sum,
            "open_mismatches": open_mismatches,
            "pending_bills": pending_bills_count,
            "total_billed": round(total_billed, 2),
            "total_paid": round(total_paid, 2),
            "outstanding_amount": round(outstanding_amt, 2),
        },
        "gatepasses": gps,
        "deliveries": deliveries,
        "mismatches": mismatches_list,
        "pending_balances": [
            {"item_name": k, **v} for k, v in balances_map.items()
        ],
        "bills": bills,
        "payments": payments,
    }


# --- 2. Client-wise Report ---
@router.get(
    "/reports/client-wise",
    dependencies=[Depends(require_capability("dashboard:read"))],
)
async def get_client_wise_report():
    gp_cursor = gatepasses_collection.find()
    clients_map: Dict[str, dict] = {}

    async for doc in gp_cursor:
        try:
            gp = _decrypt_gp(doc)
            c_label = gp["client_name"].strip()
            if not c_label:
                continue
            if c_label not in clients_map:
                clients_map[c_label] = {
                    "client": c_label,
                    "total_received": 0,
                    "total_delivered": 0,
                    "total_pending": 0,
                    "total_mismatches": 0,
                    "total_billed": 0.0,
                    "total_paid": 0.0,
                    "outstanding": 0.0,
                    "gate_passes_count": 0,
                }
            clients_map[c_label]["gate_passes_count"] += 1
            for item in gp.get("items", []):
                clients_map[c_label]["total_received"] += item.get("received_qty", 0)
                if item.get("difference", 0) != 0:
                    clients_map[c_label]["total_mismatches"] += 1
        except Exception:
            continue

    del_cursor = deliveries_collection.find({"status": {"$ne": "CANCELLED"}})
    async for doc in del_cursor:
        try:
            dl = _decrypt_del(doc)
            client = dl["client_name"].strip()
            if client in clients_map:
                for item in dl.get("items", []):
                    clients_map[client]["total_delivered"] += item.get("quantity", 0)
        except Exception:
            continue

    bill_cursor = bills_collection.find({"payment_status": {"$ne": "CANCELLED"}})
    async for doc in bill_cursor:
        try:
            bill = _decrypt_bill(doc)
            client = bill["client_name"].strip()
            if client in clients_map:
                clients_map[client]["total_billed"] += bill.get("grand_total", 0.0)
                clients_map[client]["total_paid"] += bill.get("paid_amount", 0.0)
                clients_map[client]["outstanding"] += bill.get("outstanding_amount", 0.0)
        except Exception:
            continue

    results = []
    for c_label, stats in clients_map.items():
        stats["total_pending"] = max(0, stats["total_received"] - stats["total_delivered"])
        stats["total_billed"] = round(stats["total_billed"], 2)
        stats["total_paid"] = round(stats["total_paid"], 2)
        stats["outstanding"] = round(stats["outstanding"], 2)
        results.append(stats)

    return results


# --- 3. Item-wise Report ---
@router.get(
    "/reports/item-wise",
    dependencies=[Depends(require_capability("dashboard:read"))],
)
async def get_item_wise_report():
    gp_cursor = gatepasses_collection.find()
    items_map: Dict[str, dict] = {}

    async for doc in gp_cursor:
        try:
            gp = _decrypt_gp(doc)
            for item in gp.get("items", []):
                name = item["item_name"]
                if name not in items_map:
                    items_map[name] = {"item": name, "received": 0, "delivered": 0, "pending": 0}
                items_map[name]["received"] += item.get("received_qty", 0)
        except Exception:
            pass

    del_cursor = deliveries_collection.find({"status": {"$ne": "CANCELLED"}})
    async for doc in del_cursor:
        try:
            dl = _decrypt_del(doc)
            for item in dl.get("items", []):
                name = item["item_name"]
                if name in items_map:
                    items_map[name]["delivered"] += item.get("quantity", 0)
        except Exception:
            pass

    for name, stats in items_map.items():
        stats["pending"] = max(0, stats["received"] - stats["delivered"])

    return list(items_map.values())


# --- 4. Gate Pass-wise Report ---
@router.get(
    "/reports/gatepass-wise",
    dependencies=[Depends(require_capability("dashboard:read"))],
)
async def get_gatepass_wise_report():
    gp_cursor = gatepasses_collection.find().sort("receiving_date", -1)
    results = []

    async for doc in gp_cursor:
        try:
            gp = _decrypt_gp(doc)
            gp_id = gp["id"]

            expected = sum(x.get("client_qty", 0) for x in gp.get("items", []))
            received = sum(x.get("received_qty", 0) for x in gp.get("items", []))
            difference = received - expected

            del_ids = []
            del_cursor_2 = deliveries_collection.find(
                {"gate_pass_id": gp_id, "status": {"$ne": "CANCELLED"}}
            )
            delivered = 0
            async for d_doc in del_cursor_2:
                try:
                    dl = _decrypt_del(d_doc)
                    del_ids.append(dl["id"])
                    delivered += sum(x.get("quantity", 0) for x in dl.get("items", []))
                except Exception:
                    pass

            pending = max(0, received - delivered)

            billing_status = "UNBILLED"
            if del_ids:
                bill_doc = await bills_collection.find_one(
                    {
                        "delivery_ids": {"$in": del_ids},
                        "payment_status": {"$ne": "CANCELLED"},
                    }
                )
                if bill_doc:
                    billing_status = "BILLED"

            results.append(
                {
                    "gate_pass_number": gp["gate_pass_number"],
                    "client_name": gp["client_name"],
                    "receiving_date": gp.get("receiving_date"),
                    "expected": expected,
                    "received": received,
                    "difference": difference,
                    "delivered": delivered,
                    "pending": pending,
                    "billing_status": billing_status,
                }
            )
        except Exception:
            pass

    return results


# --- 5. Billing Summary Report ---
@router.get(
    "/reports/billing",
    dependencies=[Depends(require_capability("dashboard:read"))],
)
async def get_billing_report():
    cursor = bills_collection.find({"payment_status": {"$ne": "CANCELLED"}})
    total_sales = 0.0
    pending_amount = 0.0
    paid_amount = 0.0
    outstanding_amt = 0.0

    async for doc in cursor:
        try:
            bill = _decrypt_bill(doc)
            total_sales += bill.get("grand_total", 0.0)
            paid_amount += bill.get("paid_amount", 0.0)
            outstanding_amt += bill.get("outstanding_amount", 0.0)
            if bill.get("payment_status") != "PAID":
                pending_amount += bill.get("outstanding_amount", 0.0)
        except Exception:
            pass

    return {
        "total_sales": round(total_sales, 2),
        "pending_bills_amount": round(pending_amount, 2),
        "paid_bills_amount": round(paid_amount, 2),
        "outstanding_amount": round(outstanding_amt, 2),
    }


# --- 6. Audit Logs ---
@router.get(
    "/audit-logs",
    dependencies=[Depends(require_capability("dashboard:read"))],
)
async def get_audit_logs(
    limit: int = Query(50, ge=1, le=200),
):
    cursor = audit_collection.find().sort("timestamp", -1).limit(limit)
    logs = []
    async for doc in cursor:
        doc["id"] = str(doc["_id"])
        del doc["_id"]
        logs.append(doc)
    return logs

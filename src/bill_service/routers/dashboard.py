from datetime import datetime, timezone
from typing import Dict, List, Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, get_search_token
from ..database.main_db import (
    audit_collection,
    bills_collection,
    deliveries_collection,
    gatepasses_collection,
    payments_collection,
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
        "recent_gate_passes": gps,  # Include both for compatibility
        "deliveries": deliveries,
        "mismatches": mismatches_list,
        "pending_balances": [
            {"item_name": k, **v} for k, v in balances_map.items()
        ],
        "bills": bills,
        "recent_bills": bills,  # Include both for compatibility
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
                    "client_name": c_label,
                    "total_received": 0,
                    "total_delivered": 0,
                    "total_pending": 0,
                    "total_mismatches": 0,
                    "total_billed": 0.0,
                    "paid_amount": 0.0,
                    "outstanding": 0.0,
                    "gate_pass_count": 0,
                }
            clients_map[c_label]["gate_pass_count"] += 1
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
                clients_map[client]["paid_amount"] += bill.get("paid_amount", 0.0)
                clients_map[client]["outstanding"] += bill.get("outstanding_amount", 0.0)
        except Exception:
            continue

    results = []
    for c_label, stats in clients_map.items():
        stats["total_pending"] = max(0, stats["total_received"] - stats["total_delivered"])
        stats["total_billed"] = round(stats["total_billed"], 2)
        stats["paid_amount"] = round(stats["paid_amount"], 2)
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
                    items_map[name] = {
                        "item_name": name,
                        "total_received": 0,
                        "total_delivered": 0,
                        "pending": 0,
                        "mismatch_count": 0,
                        "clients": set()
                    }
                items_map[name]["total_received"] += item.get("received_qty", 0)
                if item.get("difference", 0) != 0:
                    items_map[name]["mismatch_count"] += 1
                items_map[name]["clients"].add(gp.get("client_name", ""))
        except Exception:
            pass

    del_cursor = deliveries_collection.find({"status": {"$ne": "CANCELLED"}})
    async for doc in del_cursor:
        try:
            dl = _decrypt_del(doc)
            for item in dl.get("items", []):
                name = item["item_name"]
                if name in items_map:
                    items_map[name]["total_delivered"] += item.get("quantity", 0)
        except Exception:
            pass

    results = []
    for name, stats in items_map.items():
        stats["pending"] = max(0, stats["total_received"] - stats["total_delivered"])
        stats["client_count"] = len(stats["clients"])
        del stats["clients"]  # Remove the set before returning
        results.append(stats)

    return results


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

            # Calculate totals per gate pass
            total_received = sum(x.get("received_qty", 0) for x in gp.get("items", []))
            mismatch_count = sum(1 for x in gp.get("items", []) if x.get("difference", 0) != 0)

            # Get deliveries for this gate pass
            del_cursor_2 = deliveries_collection.find(
                {"gate_pass_id": gp_id, "status": {"$ne": "CANCELLED"}}
            )
            total_delivered = 0
            async for d_doc in del_cursor_2:
                try:
                    dl = _decrypt_del(d_doc)
                    total_delivered += sum(x.get("quantity", 0) for x in dl.get("items", []))
                except Exception:
                    pass

            # Calculate current balance
            current_balance = max(0, total_received - total_delivered)

            # Item-wise balance breakdown
            item_balances = {}
            for item in gp.get("items", []):
                item_name = item["item_name"]
                item_balances[item_name] = {
                    "received": item.get("received_qty", 0),
                    "delivered": 0,
                    "balance": item.get("received_qty", 0)
                }

            # Deduct delivered quantities
            del_cursor_3 = deliveries_collection.find(
                {"gate_pass_id": gp_id, "status": {"$ne": "CANCELLED"}}
            )
            async for d_doc in del_cursor_3:
                try:
                    dl = _decrypt_del(d_doc)
                    for item in dl.get("items", []):
                        item_name = item["item_name"]
                        qty = item.get("quantity", 0)
                        if item_name in item_balances:
                            item_balances[item_name]["delivered"] += qty
                            item_balances[item_name]["balance"] = max(
                                0, 
                                item_balances[item_name]["received"] - item_balances[item_name]["delivered"]
                            )
                except Exception:
                    pass

            results.append(
                {
                    "gate_pass_id": gp_id,
                    "gate_pass_number": gp["gate_pass_number"],
                    "client_name": gp["client_name"],
                    "receiving_date": gp.get("receiving_date"),
                    "received_by": gp.get("received_by"),
                    "total_received": total_received,
                    "total_delivered": total_delivered,
                    "current_balance": current_balance,
                    "mismatch_count": mismatch_count,
                    "status": gp.get("status"),
                    "item_balances": [
                        {"item_name": name, **balances} 
                        for name, balances in item_balances.items()
                    ]
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


# --- 6. Comprehensive Business Dashboard ---
@router.get(
    "/dashboard/overview",
    dependencies=[Depends(require_capability("dashboard:read"))],
)
async def get_comprehensive_dashboard(
    period: str = Query("month", regex="^(day|week|month|quarter|year)$"),
    _user: dict = Depends(get_current_user)
):
    """
    Comprehensive business dashboard with all essential KPIs following industry best practices.
    Research sources: Spindle Live, Modeliks, BusinessPlan Templates, Intuit ERP
    
    Key Metrics Tracked:
    - Revenue & Profitability (total sales, revenue trends)
    - Operational Efficiency (turnaround time, on-time delivery rate)
    - Client Metrics (active clients, client retention, top clients)
    - Inventory Status (items pending, inventory turnover)
    - Financial Health (outstanding payments, collection rate)
    - Quality Metrics (mismatch rate, error tracking)
    """
    from datetime import datetime, timedelta, timezone
    
    now = datetime.now(timezone.utc)
    
    # Calculate period boundaries
    period_map = {
        "day": timedelta(days=1),
        "week": timedelta(weeks=1),
        "month": timedelta(days=30),
        "quarter": timedelta(days=90),
        "year": timedelta(days=365),
    }
    period_start = now - period_map[period]
    
    # --- FINANCIAL METRICS ---
    bills_cursor = bills_collection.find({
        "payment_status": {"$ne": "CANCELLED"},
        "bill_date": {"$gte": period_start}
    })
    
    total_revenue = 0.0
    total_paid = 0.0
    total_outstanding = 0.0
    bill_count = 0
    paid_bills = 0
    
    revenue_by_date = {}
    
    async for doc in bills_cursor:
        try:
            bill = _decrypt_bill(doc)
            grand_total = bill.get("grand_total", 0.0)
            paid_amt = bill.get("paid_amount", 0.0)
            outstanding = bill.get("outstanding_amount", 0.0)
            
            total_revenue += grand_total
            total_paid += paid_amt
            total_outstanding += outstanding
            bill_count += 1
            
            if bill.get("payment_status") == "PAID":
                paid_bills += 1
            
            # Revenue trend
            bill_date = bill.get("bill_date")
            if isinstance(bill_date, datetime):
                date_key = bill_date.strftime("%Y-%m-%d")
                revenue_by_date[date_key] = revenue_by_date.get(date_key, 0) + grand_total
                
        except Exception:
            pass
    
    # Collection rate (% of revenue collected)
    collection_rate = (total_paid / total_revenue * 100) if total_revenue > 0 else 0
    
    # Average bill value
    avg_bill_value = total_revenue / bill_count if bill_count > 0 else 0
    
    # --- OPERATIONAL METRICS ---
    gp_cursor = gatepasses_collection.find({"receiving_date": {"$gte": period_start}})
    
    total_items_received = 0
    total_items_delivered = 0
    gate_pass_count = 0
    mismatch_count = 0
    total_mismatch_items = 0
    
    client_set = set()
    turnaround_times = []
    
    async for doc in gp_cursor:
        try:
            gp = _decrypt_gp(doc)
            gp_id = gp["id"]
            gate_pass_count += 1
            
            client_set.add(gp["client_name"])
            
            # Count received items
            for item in gp.get("items", []):
                received_qty = item.get("received_qty", 0)
                total_items_received += received_qty
                total_mismatch_items += received_qty
                
                if item.get("difference", 0) != 0:
                    mismatch_count += 1
            
            # Count delivered items and calculate turnaround time
            del_cursor = deliveries_collection.find({
                "gate_pass_id": gp_id,
                "status": {"$ne": "CANCELLED"}
            })
            
            first_delivery_date = None
            async for d_doc in del_cursor:
                try:
                    dl = _decrypt_del(d_doc)
                    for item in dl.get("items", []):
                        total_items_delivered += item.get("quantity", 0)
                    
                    delivery_date = dl.get("delivery_date")
                    if isinstance(delivery_date, datetime):
                        if first_delivery_date is None or delivery_date < first_delivery_date:
                            first_delivery_date = delivery_date
                except Exception:
                    pass
            
            # Calculate turnaround time (receiving to first delivery)
            receiving_date = gp.get("receiving_date")
            if isinstance(receiving_date, datetime) and first_delivery_date:
                turnaround = (first_delivery_date - receiving_date).total_seconds() / 3600  # hours
                turnaround_times.append(turnaround)
                
        except Exception:
            pass
    
    # Average turnaround time (hours)
    avg_turnaround_hours = sum(turnaround_times) / len(turnaround_times) if turnaround_times else 0
    
    # Mismatch rate (quality metric)
    mismatch_rate = (mismatch_count / total_mismatch_items * 100) if total_mismatch_items > 0 else 0
    
    # On-time delivery rate (items delivered vs received)
    delivery_fulfillment_rate = (total_items_delivered / total_items_received * 100) if total_items_received > 0 else 0
    
    # Inventory turnover (how many times inventory cycles through)
    pending_items = max(0, total_items_received - total_items_delivered)
    inventory_turnover = (total_items_delivered / pending_items) if pending_items > 0 else 0
    
    # --- CLIENT METRICS ---
    active_clients = len(client_set)
    
    # Get all-time client data for retention calculation
    all_gp_cursor = gatepasses_collection.find()
    all_clients = set()
    client_last_activity = {}
    
    async for doc in all_gp_cursor:
        try:
            gp = _decrypt_gp(doc)
            client = gp["client_name"]
            all_clients.add(client)
            
            receiving_date = gp.get("receiving_date")
            if isinstance(receiving_date, datetime):
                if client not in client_last_activity or receiving_date > client_last_activity[client]:
                    client_last_activity[client] = receiving_date
        except Exception:
            pass
    
    # Client retention (clients active in last 90 days)
    retention_cutoff = now - timedelta(days=90)
    retained_clients = sum(1 for date in client_last_activity.values() if date >= retention_cutoff)
    client_retention_rate = (retained_clients / len(all_clients) * 100) if all_clients else 0
    
    # --- TOP PERFORMERS ---
    # Top 5 clients by revenue
    client_revenue = {}
    all_bills_cursor = bills_collection.find({"payment_status": {"$ne": "CANCELLED"}})
    
    async for doc in all_bills_cursor:
        try:
            bill = _decrypt_bill(doc)
            client = bill["client_name"]
            revenue = bill.get("grand_total", 0.0)
            client_revenue[client] = client_revenue.get(client, 0) + revenue
        except Exception:
            pass
    
    top_clients = sorted(
        [{"client_name": k, "revenue": round(v, 2)} for k, v in client_revenue.items()],
        key=lambda x: x["revenue"],
        reverse=True
    )[:5]
    
    # Revenue trend data (for charts)
    revenue_trend = sorted(
        [{"date": k, "revenue": round(v, 2)} for k, v in revenue_by_date.items()],
        key=lambda x: x["date"]
    )
    
    # --- ALERTS & CRITICAL METRICS ---
    critical_alerts = []
    
    # Alert: High outstanding amount
    if total_outstanding > total_revenue * 0.3:  # More than 30% outstanding
        critical_alerts.append({
            "type": "high_outstanding",
            "severity": "high",
            "message": f"Outstanding amount ({round(total_outstanding, 2)}) exceeds 30% of revenue",
            "value": round(total_outstanding, 2)
        })
    
    # Alert: Low collection rate
    if collection_rate < 70:
        critical_alerts.append({
            "type": "low_collection",
            "severity": "high",
            "message": f"Collection rate ({round(collection_rate, 1)}%) is below 70%",
            "value": round(collection_rate, 1)
        })
    
    # Alert: High mismatch rate
    if mismatch_rate > 5:
        critical_alerts.append({
            "type": "high_mismatch",
            "severity": "medium",
            "message": f"Mismatch rate ({round(mismatch_rate, 1)}%) exceeds 5% quality threshold",
            "value": round(mismatch_rate, 1)
        })
    
    # Alert: Large pending inventory
    if pending_items > total_items_received * 0.4:
        critical_alerts.append({
            "type": "high_inventory",
            "severity": "medium",
            "message": f"{pending_items} items pending (>40% of received items)",
            "value": pending_items
        })
    
    return {
        "period": period,
        "generated_at": now.isoformat(),
        
        # === FINANCIAL METRICS ===
        "financial": {
            "total_revenue": round(total_revenue, 2),
            "total_paid": round(total_paid, 2),
            "total_outstanding": round(total_outstanding, 2),
            "collection_rate": round(collection_rate, 1),
            "avg_bill_value": round(avg_bill_value, 2),
            "total_bills": bill_count,
            "paid_bills": paid_bills,
            "pending_bills": bill_count - paid_bills,
        },
        
        # === OPERATIONAL METRICS ===
        "operations": {
            "gate_passes_processed": gate_pass_count,
            "total_items_received": total_items_received,
            "total_items_delivered": total_items_delivered,
            "pending_items": pending_items,
            "avg_turnaround_hours": round(avg_turnaround_hours, 1),
            "delivery_fulfillment_rate": round(delivery_fulfillment_rate, 1),
            "inventory_turnover": round(inventory_turnover, 2),
        },
        
        # === QUALITY METRICS ===
        "quality": {
            "mismatch_count": mismatch_count,
            "mismatch_rate": round(mismatch_rate, 2),
            "total_items_checked": total_mismatch_items,
        },
        
        # === CLIENT METRICS ===
        "clients": {
            "active_clients": active_clients,
            "total_clients": len(all_clients),
            "client_retention_rate": round(client_retention_rate, 1),
            "new_clients_this_period": active_clients,
        },
        
        # === TRENDS & CHARTS DATA ===
        "trends": {
            "revenue_by_date": revenue_trend,
            "top_clients": top_clients,
        },
        
        # === ALERTS ===
        "alerts": {
            "count": len(critical_alerts),
            "items": critical_alerts
        }
    }


# --- 8. Notifications: Items to be Sent ---
@router.get(
    "/notifications/items-to-send",
    dependencies=[Depends(require_capability("dashboard:read"))],
)
async def get_items_to_send():
    """
    Returns items that have been received but not yet delivered.
    Grouped by client with item-wise breakdown.
    """
    notifications = []
    
    # Get all gate passes with pending items
    gp_cursor = gatepasses_collection.find({"status": {"$nin": ["CANCELLED", "DELIVERED"]}}).sort("receiving_date", 1)
    
    async for doc in gp_cursor:
        try:
            gp = _decrypt_gp(doc)
            gp_id = gp["id"]
            client_name = gp["client_name"]
            
            # Build item balance map
            item_balances = {}
            for item in gp.get("items", []):
                item_name = item["item_name"]
                received_qty = item.get("received_qty", 0)
                if received_qty > 0:
                    item_balances[item_name] = {
                        "received": received_qty,
                        "delivered": 0,
                        "pending": received_qty,
                        "category": item.get("category", "")
                    }
            
            # Deduct delivered quantities
            del_cursor = deliveries_collection.find(
                {"gate_pass_id": gp_id, "status": {"$ne": "CANCELLED"}}
            )
            async for d_doc in del_cursor:
                try:
                    dl = _decrypt_del(d_doc)
                    for item in dl.get("items", []):
                        item_name = item["item_name"]
                        qty = item.get("quantity", 0)
                        if item_name in item_balances:
                            item_balances[item_name]["delivered"] += qty
                            item_balances[item_name]["pending"] = max(
                                0,
                                item_balances[item_name]["received"] - item_balances[item_name]["delivered"]
                            )
                except Exception:
                    pass
            
            # Filter items with pending quantities
            pending_items = [
                {"item_name": name, **balance}
                for name, balance in item_balances.items()
                if balance["pending"] > 0
            ]
            
            if pending_items:
                total_pending = sum(item["pending"] for item in pending_items)
                
                # Calculate days pending
                from datetime import datetime, timezone
                receiving_date = gp.get("receiving_date")
                if isinstance(receiving_date, datetime):
                    days_pending = (datetime.now(timezone.utc) - receiving_date).days
                else:
                    days_pending = 0
                
                notifications.append({
                    "gate_pass_id": gp_id,
                    "gate_pass_number": gp["gate_pass_number"],
                    "client_name": client_name,
                    "receiving_date": gp.get("receiving_date"),
                    "days_pending": days_pending,
                    "total_pending_items": total_pending,
                    "pending_items": pending_items,
                    "priority": "high" if days_pending > 7 else "medium" if days_pending > 3 else "normal"
                })
        except Exception:
            pass
    
    return {
        "count": len(notifications),
        "notifications": sorted(notifications, key=lambda x: x["days_pending"], reverse=True)
    }


# --- 9. Audit Logs ---
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

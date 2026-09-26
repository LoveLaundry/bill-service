"""Reconciliation screen: surface quantity/closure inconsistencies.

This endpoint is read-only. Fixing an issue is done through the normal
workflow (catch-up delivery, adjustments, corrections) so the state machine
and the event journal stay intact.
"""
from collections import Counter
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..crypto_helper import decrypt_dict
from ..database.main_db import bills_collection, deliveries_collection, gatepasses_collection
from ..services import balance_engine as be
from ..services import operations_context as ctx

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

GATEPASS_SENSITIVE_FIELDS = ["client_name", "items", "notes"]
DELIVERY_SENSITIVE_FIELDS = ["client_name", "items", "notes"]
BILL_SENSITIVE_FIELDS = ["items"]


@router.get("/issues")
async def reconciliation_issues(
    status: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """List gate passes that carry reconciliation issues, newest first."""
    query: dict = {}
    if status:
        query["status"] = status

    # Cache bills per gate pass id (for BILL_EXCEEDS_RECEIVED checks).
    bills_by_gp: dict = {}
    bills_cursor = bills_collection.find({"payment_status": {"$ne": "CANCELLED"}})
    async for bill_doc in bills_cursor:
        try:
            bill = decrypt_dict(bill_doc, BILL_SENSITIVE_FIELDS)
        except Exception:
            continue
        gp_id = bill.get("gate_pass_id")
        if not gp_id:
            continue
        bills_by_gp.setdefault(gp_id, []).append(bill)

    issues_out = []
    summary: Counter = Counter()

    gp_cursor = gatepasses_collection.find(query).sort("receiving_date", -1)
    async for gp_doc in gp_cursor:
        if gp_doc.get("status") == "CANCELLED":
            continue
        try:
            gp = decrypt_dict(gp_doc, GATEPASS_SENSITIVE_FIELDS)
        except Exception:
            continue
        gp_id = str(gp_doc["_id"])

        # Per-pass attribution from the shared context. The reconciliation
        # report is the audit of last resort: it must see exactly the same
        # ledger the balance screens show, so it can never disagree with them.
        # A flat per-item sum here (and a document-level gate_pass_id lookup)
        # made a multi-pass delivery look like an oversell on one pass and an
        # under-delivery on the other.
        deliveries, returns = await ctx.load_movements([gp_id])
        balance = be.compute_gate_pass_balance(
            gp.get("items", []),
            ctx.delivered_by_gate_pass(deliveries).get(gp_id, {}),
            ctx.returned_by_gate_pass(returns).get(gp_id, {}),
            marked_delivered=bool(gp.get("marked_delivered")),
        )

        # Billed quantities per item name, summed across this pass's bills
        # (GP-leg bills reference gate_pass_id; delivery-leg bills reference
        # the pass's deliveries). Keep it simple: aggregate only GP-leg bills,
        # since delivery-leg bills reference delivery ids that we would have to
        # map back through deliveries we already have here.
        billed_by_name = Counter()
        for bill in bills_by_gp.get(gp_id, []):
            for item in bill.get("items", []):
                billed_by_name[item.get("item_name", "")] += item.get("quantity", 0)

        issues = be.detect_reconciliation_issues(
            balance,
            gp_doc.get("status", ""),
            bool(gp.get("marked_delivered")),
            dict(billed_by_name),
        )
        if issues:
            codes = [i["code"] for i in issues]
            for c in codes:
                summary[c] += 1
            issues_out.append(
                {
                    "id": gp_id,
                    "gate_pass_number": gp.get("gate_pass_number"),
                    "client_name": gp.get("client_name"),
                    "status": gp_doc.get("status", ""),
                    "receiving_date": gp.get("receiving_date"),
                    "legacy_marked": bool(gp.get("marked_delivered")),
                    "totals": balance["totals"],
                    "issues": issues,
                }
            )

    return {"items": issues_out, "summary": dict(summary)}
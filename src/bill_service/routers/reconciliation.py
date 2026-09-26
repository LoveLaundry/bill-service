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
from ..database.main_db import (
    balance_adjustments_collection,
    bills_collection,
    deliveries_collection,
    gatepasses_collection,
    returns_collection,
)
from ..services import balance_engine as be

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
    unreadable_returns = 0

    gp_cursor = gatepasses_collection.find(query).sort("receiving_date", -1)
    async for gp_doc in gp_cursor:
        if gp_doc.get("status") == "CANCELLED":
            continue
        try:
            gp = decrypt_dict(gp_doc, GATEPASS_SENSITIVE_FIELDS)
        except Exception:
            continue
        gp_id = str(gp_doc["_id"])

        deliveries = []
        del_cursor = deliveries_collection.find(
            {"gate_pass_id": gp_id, "status": {"$ne": "CANCELLED"}}
        )
        async for del_doc in del_cursor:
            try:
                deliveries.append(decrypt_dict(del_doc, DELIVERY_SENSITIVE_FIELDS))
            except Exception:
                continue

        adjustments = []
        async for adj_doc in balance_adjustments_collection.find({"gate_pass_id": gp_id}):
            adjustments.append(adj_doc)

        # Returns live on their own collection keyed by the pass they were
        # raised on, and their `items` are envelope-encrypted. They used to be
        # dropped here, so a piece the client handed back still counted as
        # outstanding and the pass got flagged for an issue it did not have —
        # and when they WERE read they were read raw, so `items` came back as
        # ciphertext and contributed nothing either way.
        return_docs = []
        return_cursor = returns_collection.find({"gate_pass_id": gp_id})
        async for ret_doc in return_cursor:
            try:
                return_docs.append(decrypt_dict(ret_doc, GATEPASS_SENSITIVE_FIELDS))
            except Exception:
                unreadable_returns += 1
                continue

        balance = be.compute_gate_pass_balance(
            gp.get("items", []),
            be.compute_delivered_by_item(deliveries),
            be.compute_returned_by_item(return_docs),
            marked_delivered=bool(gp.get("marked_delivered")),
            balance_adjustment_by_item=be.compute_balance_adjustments_by_item(
                adjustments
            ),
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

    return {
        "items": issues_out,
        "summary": dict(summary),
        # A return that will not decrypt is invisible to the balance, so the
        # screen must be able to say so rather than quietly under-report.
        "unreadable_returns": unreadable_returns,
    }

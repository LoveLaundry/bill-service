"""Notification feed built from canonical balances.

This exists because the quantity arithmetic that powers the bell icon used to
live in the browser, duplicated across the notification hook, the today page and
the gate-pass detail screen. Each copy derived "pending" slightly differently,
so the badge, the dialog and the delivery form could disagree about the same
gate pass in the same session.

Everything here is derived from the one balance engine the rest of the service
uses, so a notification can never claim a pass is fully delivered while the
delivery form still offers quantity against it.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..crypto_helper import decrypt_dict, get_search_token
from ..database.main_db import gatepasses_collection
from ..services import balance_engine as be
from ..services import operations_context as ctx

router = APIRouter(tags=["notifications"])

SENSITIVE_FIELDS_GP = ["client_name", "items", "notes"]


def _serialize_gp(doc: dict) -> dict:
    dec = decrypt_dict(doc, SENSITIVE_FIELDS_GP)
    dec["id"] = str(dec["_id"])
    dec.pop("_id", None)
    return dec


@router.get("/gatepass-pending")
async def gatepass_pending(
    client_name: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """One entry per gate-pass line that still owes linen to the client.

    ``received`` / ``delivered`` / ``pending`` are server-computed, so the badge
    count, the notification dialog and the delivery form can never drift apart.
    """
    query: dict = {"status": {"$ne": "CANCELLED"}}
    if client_name:
        query["client_name_search"] = get_search_token(client_name)

    docs = await gatepasses_collection.find(query).to_list(length=None)
    if not docs:
        return []

    gps = [_serialize_gp(d) for d in docs]
    gp_ids = [g["id"] for g in gps]
    movements, returns = await ctx.load_movements(gp_ids)
    balances = ctx.balances_for(
        gps,
        ctx.delivered_by_gate_pass(movements),
        ctx.returned_by_gate_pass(returns),
    )

    entries: List[dict] = []
    for gp in gps:
        balance = balances.get(gp["id"])
        if not balance:
            continue
        # `items` is keyed by item_key, so the same name under two
        # specifications stays two separate rows here.
        for row in balance["items"].values():
            outstanding = row.get("outstanding_delivery_qty", 0)
            if outstanding <= 0:
                continue
            entries.append(
                {
                    "gate_pass_id": gp["id"],
                    "gate_pass_number": gp.get("gate_pass_number"),
                    "client_name": gp.get("client_name"),
                    # Echoed so the close-day screen can scope outstanding
                    # quantity to the day a pass was received, without
                    # rebuilding the balance per pass.
                    "receiving_date": gp.get("receiving_date"),
                    "item_name": row.get("item_name"),
                    "specification": row.get("specification") or None,
                    "item_key": row.get("item_key"),
                    "received": row.get("received_qty", 0),
                    "delivered": row.get("delivered_qty", 0),
                    "returned": row.get("returned_back_qty", 0),
                    "pending": outstanding,
                    "status": ctx.derived_status(gp, balance),
                }
            )

    # Most urgent first: the biggest shortfall, then oldest pass.
    entries.sort(
        key=lambda e: (
            -e["pending"],
            e.get("gate_pass_number") or "",
        )
    )
    return entries


@router.get("/summary")
async def notification_summary(
    client_name: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("gatepass:read")),
):
    """Counts for the bell badge, computed from the same entries as the feed."""
    entries = await gatepass_pending(client_name=client_name, current_user=current_user)
    pass_ids = {e["gate_pass_id"] for e in entries}
    return {
        "pending_pieces": sum(e["pending"] for e in entries),
        "pending_items": len(entries),
        "pending_gate_passes": len(pass_ids),
    }


__all__ = ["router", "be"]

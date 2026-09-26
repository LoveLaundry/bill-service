"""One loader for "what is the balance of this gate pass right now".

Delivery creation, catch-up delivery, the print report and the balance screen
all need the same thing: the gate pass, plus every delivery, return and balance
correction already recorded against it, so the balance engine can be run over
them.

They each used to re-derive it by hand, and each hand-rolled version was wrong
in a different way -- one ignored returns, one ignored corrections, one
accumulated a payload's duplicates and another did not. Every caller now comes
through here so there is a single aggregation to get right and test.

Deliveries and returns are envelope-encrypted, so each document is decrypted on
the way in. A document that will not decrypt is skipped rather than aborting the
whole read: a single unreadable row must not make the gate pass unviewable. That
does mean such a row is invisible to the balance, which is why the caller can
ask for the count of skipped rows.
"""
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException

from .crypto_helper import decrypt_dict
from .database.main_db import (
    balance_adjustments_collection,
    deliveries_collection,
    gatepasses_collection,
    returns_collection,
)
from .services import balance_engine as be

GATEPASS_SENSITIVE_FIELDS = ["client_name", "items", "notes"]


class GatePassBalanceContext(NamedTuple):
    gate_pass_oid: ObjectId
    gate_pass: dict
    balance: dict
    delivered_by_item: dict
    returned_by_item: dict
    balance_adjustment_by_item: dict
    deliveries: List[dict]
    adjustment_docs: List[dict]
    unreadable_documents: int


async def _load_gate_pass(gate_pass_id: str):
    try:
        oid = ObjectId(gate_pass_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid gate pass ID")

    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    try:
        return oid, decrypt_dict(doc, GATEPASS_SENSITIVE_FIELDS)
    except Exception:
        raise HTTPException(
            status_code=500, detail="Gate pass could not be read; it may be corrupt."
        )


def _decrypt_or_none(doc: dict, sensitive_fields: List[str]):
    try:
        return decrypt_dict(doc, sensitive_fields)
    except Exception:
        return None


async def load_gate_pass_balance_context(
    gate_pass_id: str, marked_delivered: bool = False
) -> GatePassBalanceContext:
    """Load everything the engine needs and run it.

    ``marked_delivered`` mirrors the legacy "operator ticked it off by hand"
    flag, which is retained only so old records still read the way they always
    have. It is not a shortcut for skipping movement figures.
    """
    gp_oid, gp_dec = await _load_gate_pass(gate_pass_id)

    deliveries: List[dict] = []
    unreadable = 0
    async for doc in deliveries_collection.find(
        {"gate_pass_id": gate_pass_id, "status": {"$ne": "CANCELLED"}}
    ):
        decrypted = _decrypt_or_none(doc, GATEPASS_SENSITIVE_FIELDS)
        if decrypted is None:
            unreadable += 1
            continue
        deliveries.append(decrypted)

    returns: List[dict] = []
    async for doc in returns_collection.find({"gate_pass_id": gate_pass_id}):
        decrypted = _decrypt_or_none(doc, GATEPASS_SENSITIVE_FIELDS)
        if decrypted is None:
            unreadable += 1
            continue
        returns.append(decrypted)

    adjustment_docs: List[dict] = []
    async for doc in balance_adjustments_collection.find({"gate_pass_id": gate_pass_id}):
        adjustment_docs.append(doc)

    delivered_by_item = be.compute_delivered_by_item(deliveries)
    returned_by_item = be.compute_returned_by_item(returns)
    balance_adjustment_by_item = be.compute_balance_adjustments_by_item(adjustment_docs)
    balance = be.compute_gate_pass_balance(
        gp_dec.get("items", []),
        delivered_by_item,
        returned_by_item,
        marked_delivered=marked_delivered,
        balance_adjustment_by_item=balance_adjustment_by_item,
    )

    return GatePassBalanceContext(
        gate_pass_oid=gp_oid,
        gate_pass=gp_dec,
        balance=balance,
        delivered_by_item=delivered_by_item,
        returned_by_item=returned_by_item,
        balance_adjustment_by_item=balance_adjustment_by_item,
        deliveries=deliveries,
        adjustment_docs=adjustment_docs,
        unreadable_documents=unreadable,
    )


def outstanding_for(context: GatePassBalanceContext, name: str, spec: Optional[str]):
    """Outstanding quantity for one item, as the engine sees it.

    Returns ``(outstanding, received, already_delivered)`` so a caller can
    explain the limit in the operator's terms rather than just refusing.
    """
    row = context.balance["items"].get(be.item_key(name, spec), {})
    return (
        int(row.get("outstanding_delivery_qty", 0) or 0),
        int(row.get("received_qty", 0) or 0),
        int(row.get("delivered_qty", 0) or 0),
    )


async def resync_gate_pass_status(gate_pass_id: str) -> str:
    """Re-derive a gate pass's status from its movements and persist any change.

    Every path that moves a piece — a delivery, a return, a posted or voided
    balance correction, an approved received-qty correction — has to end here.
    A pass whose status is left behind its own balance is the reason a balanced
    pass stays hidden: ``GET /deliveries/pending-gatepasses`` and the delivery
    form both key off the status, so a pass still labelled DELIVERED after a
    return never becomes selectable again.

    Returns the status now stored on the pass, whether or not it moved.
    """
    context = await load_gate_pass_balance_context(gate_pass_id)
    current = context.gate_pass.get("status", "RECEIVED")
    derived = be.derive_gate_pass_status(context.balance, current)
    if derived == current:
        return current

    now = datetime.now(timezone.utc)
    await gatepasses_collection.update_one(
        {"_id": context.gate_pass_oid},
        {"$set": {"status": derived, "updated_at": now}},
    )

    from .repositories.main_repository import bump_version, enqueue_sync

    version = await bump_version("gatepass", context.gate_pass_oid)
    await enqueue_sync("gatepass", context.gate_pass_oid, version)
    return derived

"""Automatic propagation of gate-pass item corrections to linked bills.

Bills snapshot their line items at creation, and the billable basis is the
gate pass *received* quantity (received minus anything already billed
elsewhere for the same gate pass / deliveries). When a gate pass's items are
corrected later — either through the controlled adjustment workflow or a
direct edit before any movement exists — this service re-syncs every linked,
still-editable bill so no line charges more than the corrected received
quantity.

Rules (financial safety first):

* Only editable bills (payment_status not PAID/CANCELLED) are rewritten.
  Item quantities are clamped down to the corrected received quantity; lines
  that no longer exist on the gate pass are removed. Bills are never
  increased automatically (a deliberately partial bill must not be inflated,
  and auto-adds could double-bill), so an under-billed item is reported in
  the bill note + journal instead.
* PAID/CANCELLED bills are never rewritten. A discrepancy note + journal
  event flag them for manual correction (void / credit) so money is never
  silently altered.
* Every change is version-bumped, enqueued for replication, journaled with
  item deltas, and audited — matching the manual edit path.
"""
from datetime import datetime, timezone
from typing import Optional

from ..crypto_helper import decrypt_dict, encrypt_dict
from ..database.main_db import audit_collection, bills_collection, deliveries_collection
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_BILL_SYNCED,
)
from ..services.verification_service import attach_verification_to

BILL_SENSITIVE_FIELDS = ["client_name", "quotation_title", "notes", "items"]

# payment_status values that may never be auto-rewritten.
NON_EDITABLE_STATUSES = ("PAID", "CANCELLED")


def _item_key(name: str, spec: Optional[str]) -> str:
    return f"{name}::{spec or ''}"


def _snapshot_received(gp_items: list[dict]) -> dict:
    """Map item key -> corrected received quantity on the gate pass."""
    out = {}
    for it in gp_items or []:
        key = _item_key(it.get("item_name", ""), it.get("specification"))
        out[key] = {
            "item_name": it.get("item_name", ""),
            "specification": it.get("specification"),
            "category": it.get("category"),
            "received_qty": int(it.get("received_qty", 0) or 0),
        }
    return out


async def _write_audit(
    user_id: str,
    action: str,
    entity: str,
    entity_id: str,
    details: Optional[dict] = None,
):
    audit_doc: dict = {
        "user_id": user_id,
        "action": action,
        "entity": entity,
        "entity_id": entity_id,
        "timestamp": datetime.now(timezone.utc),
    }
    if details:
        audit_doc["details"] = details
    await audit_collection.insert_one(audit_doc)


def _recompute_totals(dec: dict, new_items: list[dict]) -> tuple[float, float, float]:
    """Mirror the manual bill-edit recalc (bills.py edit_bill / create_bill)."""
    total_amount = round(sum(i["line_total"] for i in new_items), 2)
    discounts = dec.get("discounts", 0) or 0
    transport = dec.get("transport_fee", 0) or 0
    taxes = dec.get("taxes", 0) or 0
    additional = dec.get("additional_charges", 0) or 0
    grand_total = round(total_amount - discounts + transport + taxes + additional, 2)
    if grand_total < 0:
        grand_total = 0.0
    total_quantity = float(sum(i["quantity"] for i in new_items))
    return grand_total, total_quantity, total_amount


def _append_note(dec: dict, line: str) -> str:
    note_parts = [p for p in (dec.get("notes") or "").split("\n") if p.strip()]
    note_parts.append(line)
    return "\n".join(note_parts)


async def _persist_bill(
    bill_id,
    dec: dict,
    updates: dict,
    deltas: list,
    *,
    gate_pass_id: str,
    user_id: str,
    user_name: Optional[str],
    reason: Optional[str],
    meta: dict,
) -> None:
    encrypted = encrypt_dict({**dec, **updates}, BILL_SENSITIVE_FIELDS)
    await bills_collection.replace_one({"_id": bill_id}, encrypted)

    new_version = await bump_version("bill", bill_id)
    await enqueue_sync("bill", bill_id, new_version)
    await attach_verification_to("bill", bill_id, {"id": str(bill_id)})

    await record_event(
        entity_type="bill",
        entity_id=str(bill_id),
        event_type=EVENT_BILL_SYNCED,
        gate_pass_id=gate_pass_id,
        user_id=user_id,
        user_name=user_name,
        item_deltas=deltas or None,
        reason=reason,
        meta=meta,
    )
    await _write_audit(
        user_id or "system",
        "BILL_SYNCED",
        "bill",
        str(bill_id),
        details={"auto": True, "event": "gate_pass_correction", **meta},
    )


async def sync_bills_to_gate_pass(
    gate_pass_id: str,
    gp_items: list[dict],
    *,
    user_id: str = "system",
    user_name: Optional[str] = None,
    reason: Optional[str] = None,
) -> list[dict]:
    """Clamp every linked, editable bill to the corrected received quantities.

    Returns a list of per-bill outcomes:
    {"bill_id", "payment_status", "action": "adjusted"|"flagged"|"no_change"}.
    """
    received = _snapshot_received(gp_items)
    outcomes = []

    delivery_ids = []
    async for d in deliveries_collection.find(
        {"gate_pass_id": gate_pass_id, "status": {"$ne": "CANCELLED"}}
    ):
        delivery_ids.append(str(d["_id"]))

    billed_filter: dict = {
        "payment_status": {"$ne": "CANCELLED"},
        "$or": [{"gate_pass_id": gate_pass_id}],
    }
    if delivery_ids:
        billed_filter["$or"].append({"delivery_ids": {"$in": delivery_ids}})

    cursor = bills_collection.find(billed_filter)
    async for bill_doc in cursor:
        dec = decrypt_dict(bill_doc, BILL_SENSITIVE_FIELDS)
        status = dec.get("payment_status", "PENDING")
        bill_id = bill_doc["_id"]
        bill_id_str = str(bill_id)
        old_items = dec.get("items", []) or []

        deltas: list[dict] = []
        new_items: list[dict] = []
        missing_on_bill: list[str] = []

        for old in old_items:
            name = old.get("item_name", "")
            spec = old.get("specification")
            old_qty = int(old.get("quantity", 0) or 0)
            rec = received.get(_item_key(name, spec))
            new_qty = min(old_qty, rec["received_qty"]) if rec is not None else 0
            if new_qty != old_qty:
                deltas.append(build_item_delta(name, spec, old_qty, new_qty))
            if new_qty <= 0:
                continue
            new_items.append({
                "item_name": name,
                "category": old.get("category", rec.get("category") if rec else None),
                "unit_price": old.get("unit_price", 0) or 0,
                "quantity": new_qty,
                "line_total": round((old.get("unit_price", 0) or 0) * new_qty, 2),
            })

        # Items now received on the corrected gate pass but never billed on
        # this bill — reported, never auto-added (avoid over-billing).
        billed_keys = {_item_key(i["item_name"], i.get("specification")) for i in old_items}
        missing_on_bill = sorted({
            rec["item_name"]
            for key, rec in received.items()
            if rec["received_qty"] > 0 and key not in billed_keys
        })

        if status in NON_EDITABLE_STATUSES:
            corrected = ", ".join(
                f"{rec['item_name']}->{rec['received_qty']}" for rec in received.values()
            )
            note = (
                f"[auto] Gate pass {gate_pass_id} received quantities corrected: {corrected}. "
                "This paid/cancelled bill was NOT re-written — review and void/credit manually."
            )
            updates = {
                "notes": _append_note(dec, note),
                "updated_at": datetime.now(timezone.utc),
            }
            await _persist_bill(
                bill_id, dec, updates, deltas,
                gate_pass_id=gate_pass_id,
                user_id=user_id, user_name=user_name, reason=reason,
                meta={
                    "action": "flag_paid_bill",
                    "payment_status": status,
                    "corrected_received_by_item": {
                        k.split("::")[0]: rec["received_qty"] for k, rec in received.items()
                    },
                },
            )
            outcomes.append({"bill_id": bill_id_str, "payment_status": status, "action": "flagged"})
            continue

        if not deltas and not missing_on_bill:
            outcomes.append({"bill_id": bill_id_str, "payment_status": status, "action": "no_change"})
            continue

        grand_total, total_quantity, total_amount = _recompute_totals(dec, new_items)
        paid = dec.get("paid_amount", 0) or 0
        overpaid_after = round(grand_total - paid, 2)
        outstanding = max(0.0, overpaid_after)
        if overpaid_after < 0:
            overpaid_after_note = f"\n[auto] Overpaid by LKR {round(-overpaid_after, 2)} after correction."
        else:
            overpaid_after_note = ""

        note_lines = [
            f"[auto] Bill re-synced to corrected quantities on gate pass {gate_pass_id}"
            + (f" (by {user_name})" if user_name else "")
            + (f": {reason}" if reason else ""),
        ]
        if missing_on_bill:
            note_lines.append(
                f"[auto] Item(s) now received but NOT billed here: {', '.join(missing_on_bill)} — bill manually if owed."
            )
        if overpaid_after_note:
            note_lines.append(overpaid_after_note.strip())

        updates = {
            "items": new_items,
            "total_amount": total_amount,
            "total_quantity": total_quantity,
            "grand_total": grand_total,
            "outstanding_amount": outstanding,
            "notes": _append_note(dec, "\n".join(note_lines)),
            "updated_at": datetime.now(timezone.utc),
        }
        await _persist_bill(
            bill_id, dec, updates, deltas,
            gate_pass_id=gate_pass_id,
            user_id=user_id, user_name=user_name, reason=reason,
            meta={
                "action": "auto_adjusted",
                "payment_status": status,
                "grand_total_after": grand_total,
                "outstanding_after": outstanding,
                "underbilled_items": missing_on_bill or None,
                "overpaid_after_correction": round(-overpaid_after, 2) if overpaid_after < 0 else None,
            },
        )
        outcomes.append({"bill_id": bill_id_str, "payment_status": status, "action": "adjusted"})

    return outcomes
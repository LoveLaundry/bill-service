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
from ..database.main_db import (
    audit_collection,
    bills_collection,
    deliveries_collection,
    gatepasses_collection,
)
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services.transaction_events import (
    build_item_delta,
    record_event,
    EVENT_BILL_SYNCED,
)
from ..services.verification_service import attach_verification_to

BILL_SENSITIVE_FIELDS = ["client_name", "quotation_title", "notes", "items"]
GATEPASS_SENSITIVE_FIELDS = ["client_name", "items", "notes"]

# payment_status values that may never be auto-rewritten.
NON_EDITABLE_STATUSES = ("PAID", "CANCELLED")


def _object_id_or_none(value):
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _bill_gate_pass_ids(bill: dict, current_gp_id: str, delivery_to_gp: dict) -> set:
    """Every gate pass feeding this bill except the one being corrected."""
    gids = set()
    linked = bill.get("gate_pass_id")
    if linked and str(linked) != str(current_gp_id):
        gids.add(str(linked))
    for did in bill.get("delivery_ids", []) or []:
        gp_id = delivery_to_gp.get(str(did))
        if gp_id and str(gp_id) != str(current_gp_id):
            gids.add(str(gp_id))
    return gids


def _snapshot_received(gp_items: list[dict]) -> dict:
    """Map item NAME -> corrected received quantity on the gate pass.

    Bills are item-name based: ``create_bill`` aggregates specification
    variants through ``compute_billable_received_by_name`` and a bill line
    never carries a ``specification``. Keying this snapshot with
    ``name::spec`` therefore silently zeroed every spec'd bill line on
    re-sync — the snapshot must be aggregated by name exactly like the
    biller does.
    """
    out: dict = {}
    for it in gp_items or []:
        if it.get("rewashed"):
            continue  # free re-washes are never billed, so never re-synced
        name = it.get("item_name", "")
        spec_item = {
            "item_name": name,
            "category": it.get("category"),
            "received_qty": int(it.get("received_qty", 0) or 0),
        }
        cur = out.get(name)
        if cur is None:
            out[name] = spec_item
        else:
            cur["received_qty"] += spec_item["received_qty"]
            if cur["category"] is None and spec_item["category"]:
                cur["category"] = spec_item["category"]
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


def derive_payment_status(grand_total: float, paid_amount: float, current: str) -> str:
    """Payment status implied by the money, never left behind the money.

    A downward correction re-derives ``grand_total`` and ``outstanding_amount``
    but used to leave ``payment_status`` exactly as it was. A bill whose total
    fell to zero kept reading PARTIALLY_PAID, which is a bill the outstanding
    list showed forever with nothing owed. DRAFT is a pre-issue workflow state
    and ISUPDATED is a billing state, so neither is second-guessed here.
    """
    if current in ("CANCELLED", "DRAFT", "ISSUED"):
        return current
    outstanding = round(float(grand_total) - float(paid_amount), 2)
    if outstanding <= 0.01:
        return "PAID"
    if float(paid_amount) > 0:
        return "PARTIALLY_PAID"
    return "PENDING"


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
    delivery_to_gp: dict[str, str] = {}
    async for d in deliveries_collection.find(
        {"gate_pass_id": gate_pass_id, "status": {"$ne": "CANCELLED"}}
    ):
        delivery_ids.append(str(d["_id"]))

    # A bill line is the SUM of what every gate pass feeding that bill received.
    # Clamping it to this one pass's quantity silently deleted the other passes'
    # pieces -- including the whole line when the item belonged to another pass
    # entirely. Resolve each bill's full set of gate passes before clamping.
    async for d in deliveries_collection.find(
        {"status": {"$ne": "CANCELLED"}, "gate_pass_id": {"$ne": gate_pass_id}}
    ):
        delivery_to_gp[str(d["_id"])] = str(d.get("gate_pass_id"))

    other_gp_cache: dict[str, dict] = {}

    async def _other_received(gp_id: str) -> dict:
        """Received-by-name totals for one OTHER gate pass on this bill."""
        if gp_id in other_gp_cache:
            return other_gp_cache[gp_id]
        totals: dict = {}
        doc = await gatepasses_collection.find_one({"gate_pass_id": gp_id}) or await (
            gatepasses_collection.find_one({"_id": _object_id_or_none(gp_id)})
        )
        if doc:
            try:
                totals = _snapshot_received(
                    decrypt_dict(doc, GATEPASS_SENSITIVE_FIELDS).get("items", [])
                )
            except Exception:
                totals = {}
        other_gp_cache[gp_id] = totals
        return totals

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

        # Everything this bill's line is allowed to be, across all of its passes.
        other_totals: dict = {}
        for other_gp_id in _bill_gate_pass_ids(dec, gate_pass_id, delivery_to_gp):
            for other_name, other_rec in (await _other_received(other_gp_id)).items():
                other_totals[other_name] = other_totals.get(other_name, 0) + other_rec[
                    "received_qty"
                ]

        for old in old_items:
            name = old.get("item_name", "")
            spec = old.get("specification")
            old_qty = int(old.get("quantity", 0) or 0)
            rec = received.get(name)
            allowance = (rec["received_qty"] if rec is not None else 0) + other_totals.get(
                name, 0
            )
            new_qty = min(old_qty, allowance)
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
        billed_keys = {i.get("item_name", "") for i in old_items}
        missing_on_bill = sorted({
            name for name, rec in received.items()
            if rec["received_qty"] > 0 and name not in billed_keys
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
                        name: rec["received_qty"] for name, rec in received.items()
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
            # The status is a label on the same money that just moved, so it has
            # to move with it or the bill reads PARTIALLY_PAID with nothing owed.
            "payment_status": derive_payment_status(grand_total, paid, status),
            "notes": _append_note(dec, "\n".join(note_lines)),
            "updated_at": datetime.now(timezone.utc),
        }
        await _persist_bill(
            bill_id, dec, updates, deltas,
            gate_pass_id=gate_pass_id,
            user_id=user_id, user_name=user_name, reason=reason,
        meta={
            "action": "auto_adjusted",
            "payment_status": updates["payment_status"],
            "grand_total_after": grand_total,
            "outstanding_after": outstanding,
            "underbilled_items": missing_on_bill or None,
            "overpaid_after_correction": round(-overpaid_after, 2) if overpaid_after < 0 else None,
        },

        )
        outcomes.append({"bill_id": bill_id_str, "payment_status": status, "action": "adjusted"})

    return outcomes
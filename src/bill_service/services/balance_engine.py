"""Single authoritative balance engine.

Every delivered / outstanding / pending / billable number in the system
is derived from these functions so that all screens agree. Quantities are
tracked independently and NEVER derived from a note or a status label:

  expected_qty              client_qty (what the waybill said was sent)
  received_qty              what was physically received on the gate pass
  rejected_qty              damaged/rejected portion (0 by default today)
  delivered_qty             actual recorded delivery quantity (sum of delivery items)
  returned_back_qty         items the client gave back (RECEIVE_BACK / RE_WASH, not re-sent)
  outstanding_delivery_qty  received - delivered + returned_back  (still needs sending)
  not_received_qty          expected - received (>= 0), the shortage
  extra_received_qty        received - expected (>= 0), over-received quantity

The engine is pure (no database access). Callers pass decrypted documents.
"""
from typing import Dict, List, Optional


RETURN_ACTIONS_PENDING = ("RECEIVE_BACK", "RE_WASH")


def item_key(name: str, spec: Optional[str] = None) -> str:
    """Canonical per-item key. Always ``name||spec`` even when spec is empty."""
    return f"{name}||{spec or ''}"


def flatten_name(key: str) -> str:
    """Recover item name from a canonical key."""
    return key.split("||", 1)[0]


def compute_delivered_by_item(delivery_docs: List[dict]) -> Dict[str, int]:
    """Sum delivered quantities across non-cancelled delivery documents."""
    out: Dict[str, int] = {}
    for dl in delivery_docs:
        if dl.get("status") == "CANCELLED":
            continue
        for it in dl.get("items", []):
            key = item_key(it.get("item_name", ""), it.get("specification"))
            out[key] = out.get(key, 0) + int(it.get("quantity", 0) or 0)
    return out


def compute_returned_by_item(return_docs: List[dict]) -> Dict[str, int]:
    """Sum return-back quantities that still need re-sending.

    Only RECEIVE_BACK / RE_WASH items that have NOT been re-sent yet count
    as pending. Returns are attributed to the gate pass they carry; the
    caller is responsible for grouping by gate_pass_id.
    """
    out: Dict[str, int] = {}
    for ret in return_docs:
        for it in ret.get("items", []):
            if not isinstance(it, dict):
                continue
            if it.get("action") not in RETURN_ACTIONS_PENDING:
                continue
            if it.get("resend_status") == "SENT":
                continue
            key = item_key(it.get("item_name", ""), it.get("specification"))
            qty = int(it.get("returned_qty", 0) or 0)
            if qty > 0:
                out[key] = out.get(key, 0) + qty
    return out


def compute_gate_pass_balance(
    gp_items: List[dict],
    delivered_by_item: Dict[str, int],
    returned_by_item: Optional[Dict[str, int]] = None,
    *,
    marked_delivered: bool = False,
) -> dict:
    """Compute the full per-item balance for one gate pass.

    ``marked_delivered`` is the LEGACY note-based closure. When present the
    pass still counts as fully delivered for pending math (effective
    delivered = max(delivered, received)) but the item record always keeps
    ``delivered_qty`` as the REAL recorded quantity and exposes a
    ``LEGACY_NOTE_CLOSURE`` flag so the hidden balance stays visible.
    """
    returned_by_item = returned_by_item or {}
    items: Dict[str, dict] = {}
    totals = {
        "expected_qty": 0,
        "received_qty": 0,
        "delivered_qty": 0,
        "effective_delivered_qty": 0,
        "returned_back_qty": 0,
        "outstanding_delivery_qty": 0,
        "not_received_qty": 0,
    }
    gp_flags: List[str] = []
    if marked_delivered:
        gp_flags.append("MARKED_DELIVERED_LEGACY")

    for it in gp_items:
        name = it.get("item_name", "")
        spec = it.get("specification")
        key = item_key(name, spec)
        expected = int(it.get("client_qty", 0) or 0)
        received = int(it.get("received_qty", 0) or 0)
        rejected = 0
        delivered = int(delivered_by_item.get(key, 0) or 0)
        returned = int((returned_by_item or {}).get(key, 0) or 0)

        effective_delivered = max(delivered, received) if marked_delivered else delivered
        outstanding = max(0, received - effective_delivered + returned)
        not_received = max(0, expected - received)
        extra_received = max(0, received - expected)

        item_flags: List[str] = []
        if delivered > received:
            item_flags.append("DELIVERED_EXCEEDS_RECEIVED")
        if received < expected:
            item_flags.append("SHORT_RECEIVED")
        if received > expected:
            item_flags.append("EXTRA_RECEIVED")
        if marked_delivered and delivered < received:
            item_flags.append("LEGACY_NOTE_CLOSURE_HIDES_OUTSTANDING")

        items[key] = {
            "item_key": key,
            "item_name": name,
            "specification": spec or "",
            "category": it.get("category") or "",
            "expected_qty": expected,
            "received_qty": received,
            "rejected_qty": rejected,
            "accepted_qty": received - rejected,
            "delivered_qty": delivered,
            "effective_delivered_qty": effective_delivered,
            "returned_back_qty": returned,
            "outstanding_delivery_qty": outstanding,
            "not_received_qty": not_received,
            "extra_received_qty": extra_received,
            "flags": item_flags,
        }
        totals["expected_qty"] += expected
        totals["received_qty"] += received
        totals["delivered_qty"] += delivered
        totals["effective_delivered_qty"] += effective_delivered
        totals["returned_back_qty"] += returned
        totals["outstanding_delivery_qty"] += outstanding
        totals["not_received_qty"] += not_received

    return {
        "items": items,
        "totals": totals,
        "flags": gp_flags,
    }


def derive_gate_pass_status(balance: dict, current_status: str) -> str:
    """Derive the correct status from quantities, not from a manual label.

    CANCELLED stays cancelled. A pass with zero outstanding deliveries is
    DELIVERED. A pass with some real deliveries left pending is
    PARTIALLY_DELIVERED. Otherwise the workflow states
    (RECEIVED / PROCESSING / READY_FOR_DELIVERY) are kept as-is.
    """
    if current_status == "CANCELLED":
        return "CANCELLED"
    if balance["totals"]["outstanding_delivery_qty"] == 0:
        return "DELIVERED"
    if balance["totals"]["delivered_qty"] > 0:
        return "PARTIALLY_DELIVERED"
    return current_status


def compute_outstanding_per_item(gp_items: List[dict], balance: dict) -> List[dict]:
    """Per-item outstanding rows for pending lists (delivery form, dashboard)."""
    rows = []
    for key, b in balance["items"].items():
        if b["outstanding_delivery_qty"] > 0:
            rows.append(
                {
                    "item_name": b["item_name"],
                    "specification": b["specification"],
                    "category": b["category"],
                    "received_qty": b["received_qty"],
                    "delivered_qty": b["delivered_qty"],
                    "returned_qty": b["returned_back_qty"],
                    "pending_qty": b["outstanding_delivery_qty"],
                }
            )
    return rows


def compute_billable_on_received(
    gp_items: List[dict],
    billed_by_item: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Billable quantity per item based on the approved RECEIVED quantity.

    The billable event is the received quantity (business decision). Billable
    per item = received_qty - already billed quantity. Never negative.
    """
    billed_by_item = billed_by_item or {}
    out: Dict[str, int] = {}
    for it in gp_items:
        key = item_key(it.get("item_name", ""), it.get("specification"))
        received = int(it.get("received_qty", 0) or 0)
        billed = int(billed_by_item.get(key, 0) or 0)
        out[key] = max(0, received - billed)
    return out


def apply_received_correction(
    gp_items: List[dict],
    item_name: str,
    specification: Optional[str],
    corrected_qty: int,
):
    """Apply an APPROVED received-quantity correction without mutating history.

    Returns ``(updated_items, original_qty)`` or ``(None, None)`` when the
    item does not exist on the gate pass. The caller persists the original
    record in the journal; the source documents themselves are never edited.
    """
    updated: List[dict] = []
    original: Optional[int] = None
    found = False
    for it in gp_items:
        if (
            it.get("item_name") == item_name
            and (it.get("specification") or "") == (specification or "")
        ):
            original = int(it.get("received_qty", 0) or 0)
            new_item = dict(it)
            new_item["received_qty"] = corrected_qty
            try:
                new_item["difference"] = corrected_qty - int(it.get("client_qty", 0) or 0)
            except Exception:
                new_item["difference"] = 0
            updated.append(new_item)
            found = True
        else:
            updated.append(it)
    if not found:
        return None, None
    return updated, original
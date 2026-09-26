"""Single authoritative balance engine.

Every delivered / outstanding / pending / billable number in the system
is derived from these functions so that all screens agree. Quantities are
tracked independently and NEVER derived from a note or a status label:

  expected_qty              client_qty (what the waybill said was sent)
  received_qty              what was physically received on the gate pass
  rejected_qty              damaged/rejected portion (0 by default today)
  delivered_qty             actual recorded delivery quantity (sum of delivery items)
  returned_back_qty         items the client gave back (RECEIVE_BACK / RE_WASH, not re-sent)
  balance_adjustment_qty    signed correction posted when a delivery was recorded
                            wrongly (see below)
  outstanding_delivery_qty  received - delivered + returned_back + balance_adjustment
                            (still needs sending, or is owed back to the client)
  not_received_qty          expected - received (>= 0), the shortage
  extra_received_qty        received - expected (>= 0), over-received quantity

Balance adjustments (quantities only, never money):
  Sometimes a delivery is recorded with the wrong quantity, or pieces go
  missing in transit, and the pass has to be squared off. A balance
  adjustment is a SIGNED correction against one item of one gate pass:

      +3   we under-delivered / lost / damaged  -> client is owed 3 more
      -3   we over-recorded the send            -> 3 fewer are outstanding

  It is deliberately a PIECE COUNT and never a money figure: billing still
  derives from ``received_qty``, so posting an adjustment can never move an
  invoice. The balance is clamped at zero, so an adjustment can correct a
  mistake in one direction but can never make the pass look over-credited.

The engine is pure (no database access). Callers pass decrypted documents.
"""
from datetime import datetime
from typing import Dict, List, Optional


RETURN_ACTIONS_PENDING = ("RECEIVE_BACK", "RE_WASH")


def item_key(name: str, spec: Optional[str] = None) -> str:
    """Canonical per-item key. Always ``name||spec`` even when spec is empty."""
    return f"{name}||{spec or ''}"


def is_rewashed(it: dict) -> bool:
    """True when an item is tagged as a free re-wash (never billed)."""
    return bool(it.get("rewashed"))


def flatten_name(key: str) -> str:
    """Recover item name from a canonical key."""
    return key.split("||", 1)[0]


def merge_gate_pass_items(gp_items: List[dict]) -> List[dict]:
    """Collapse rows that share a name+spec into one, summing the quantities.

    Two rows for the same item (a second batch of towels, say) used to be
    keyed straight into a per-item map, where the last row silently overwrote
    the earlier one and its pieces vanished from every balance. Merging is done
    here, once, so every consumer of a gate pass's item rows sees the same
    summed figures.
    """
    merged: List[dict] = []
    merged_at: Dict[str, dict] = {}
    for it in gp_items:
        name = it.get("item_name", "")
        spec = it.get("specification")
        key = item_key(name, spec)
        existing = merged_at.get(key)
        if existing is None:
            copy = dict(it)
            merged.append(copy)
            merged_at[key] = copy
            continue
        for field in ("client_qty", "received_qty", "rejected_qty"):
            existing[field] = int(existing.get(field, 0) or 0) + int(it.get(field, 0) or 0)
        # A re-wash tag is a property of the batch, so one flagged row keeps the
        # merged row flagged rather than hiding a free re-wash.
        if is_rewashed(it):
            existing["rewashed"] = True
    return merged


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


def compute_balance_adjustments_by_item(adjustment_docs: List[dict]) -> Dict[str, int]:
    """Sum the SIGNED balance adjustments across adjustment documents.

    Each adjustment targets one item and carries ``quantity``, which may be
    negative to reduce the balance. Voided adjustments contribute nothing.
    """
    out: Dict[str, int] = {}
    for adj in adjustment_docs:
        if adj.get("status") in ("VOID", "CANCELLED"):
            continue
        qty = adj.get("quantity")
        if qty is None:
            continue
        key = item_key(adj.get("item_name", ""), adj.get("specification"))
        out[key] = out.get(key, 0) + int(qty or 0)
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
    balance_adjustment_by_item: Optional[Dict[str, int]] = None,
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
        # Signed corrections posted when a delivery was recorded wrongly.
        # Quantity-only: never reaches billing, which derives from received_qty.
        "balance_adjustment_qty": 0,
    }
    gp_flags: List[str] = []
    if marked_delivered:
        gp_flags.append("MARKED_DELIVERED_LEGACY")

    # Two rows can share a name+spec (a second batch of the same item). Their
    # quantities ADD UP: keying straight into `items` let the last row overwrite
    # the earlier ones, so received pieces silently vanished from every balance,
    # print note and status derivation. Fold them into one row instead.
    merged = merge_gate_pass_items(gp_items)

    for it in merged:
        name = it.get("item_name", "")
        spec = it.get("specification")
        key = item_key(name, spec)
        expected = int(it.get("client_qty", 0) or 0)
        received = int(it.get("received_qty", 0) or 0)
        rejected = 0
        delivered = int(delivered_by_item.get(key, 0) or 0)
        returned = int((returned_by_item or {}).get(key, 0) or 0)
        # Signed: positive credits the client, negative debits them.
        adjustment = int((balance_adjustment_by_item or {}).get(key, 0) or 0)

        effective_delivered = max(delivered, received) if marked_delivered else delivered
        # Clamped at zero so an adjustment can correct a mistake but can never
        # make a pass look over-credited.
        outstanding = max(0, received - effective_delivered + returned + adjustment)
        not_received = max(0, expected - received)
        extra_received = max(0, received - expected)

        item_flags: List[str] = []
        if is_rewashed(it):
            item_flags.append("REWASHED_FREE")
        if delivered > received:
            item_flags.append("DELIVERED_EXCEEDS_RECEIVED")
        if received < expected:
            item_flags.append("SHORT_RECEIVED")
        if received > expected:
            item_flags.append("EXTRA_RECEIVED")
        if marked_delivered and delivered < received:
            item_flags.append("LEGACY_NOTE_CLOSURE_HIDES_OUTSTANDING")
        if adjustment > 0:
            item_flags.append("BALANCE_CREDITED")
        elif adjustment < 0:
            item_flags.append("BALANCE_DEBITED")

        items[key] = {
            "item_key": key,
            "item_name": name,
            "specification": spec or "",
            "category": it.get("category") or "",
            "rewashed": is_rewashed(it),
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
            "balance_adjustment_qty": adjustment,
            "flags": item_flags,
        }
        totals["expected_qty"] += expected
        totals["received_qty"] += received
        totals["delivered_qty"] += delivered
        totals["effective_delivered_qty"] += effective_delivered
        totals["returned_back_qty"] += returned
        totals["outstanding_delivery_qty"] += outstanding
        totals["not_received_qty"] += not_received
        totals["balance_adjustment_qty"] += adjustment

    return {
        "items": items,
        "totals": totals,
        "flags": gp_flags,
    }


def recompute_status_with_movements(
    gp_items: List[dict],
    delivery_docs: List[dict],
    return_docs: List[dict],
    current_status: str,
    adjustment_docs: Optional[List[dict]] = None,
    *,
    marked_delivered: bool = False,
) -> str:
    """Derive the real status after a quantity correction, INCLUDING movements.

    The approval of a gate-pass adjustment must never re-derive status from an
    empty movement set — that would downgrade a fully-delivered pass to
    PARTIALLY_DELIVERED/RECEIVED because recorded deliveries were ignored.

    ``adjustment_docs`` must be the pass's posted balance corrections. Omitting
    them let a re-derived status ignore credits that were already applied, so a
    pass that genuinely still owed pieces came back labelled DELIVERED and
    disappeared from the delivery form.
    """
    delivered = compute_delivered_by_item(delivery_docs)
    returned = compute_returned_by_item(return_docs)
    adjustments = compute_balance_adjustments_by_item(adjustment_docs or [])
    balance = compute_gate_pass_balance(
        gp_items,
        delivered,
        returned,
        marked_delivered=marked_delivered,
        balance_adjustment_by_item=adjustments,
    )
    return derive_gate_pass_status(balance, current_status)


def has_prior_send(balance: dict) -> bool:
    """True when the pass has proof that pieces already left the premises.

    A recorded delivery is the obvious proof. A return is just as good: the
    client can only hand a piece BACK after it was sent. A balance CREDIT is
    proof too, because a credit is only ever posted against a send that was
    recorded short (under-delivered, lost or damaged in transit).

    This is decided PER ITEM. Summing the adjustments across the whole pass
    first let a debit on one item cancel out a credit on another, so a pass
    that was genuinely partly sent went back to reading RECEIVED and vanished
    from the delivery form. A debit is deliberately not proof on its own: it
    means we logged MORE as sent than was taken, so the piece never actually
    went out and the honest label for that pass is still a workflow state.
    """
    for row in balance["items"].values():
        if int(row.get("delivered_qty", 0) or 0) > 0:
            return True
        if int(row.get("returned_back_qty", 0) or 0) > 0:
            return True
        if int(row.get("balance_adjustment_qty", 0) or 0) > 0:
            return True
    return False


def derive_gate_pass_status(balance: dict, current_status: str) -> str:
    """Derive the correct status from quantities, not from a manual label.

    CANCELLED stays cancelled. A pass with zero outstanding deliveries is
    DELIVERED. A pass that still owes pieces AND has proof that something was
    already sent (a delivery, a pending return, or a balance credit) is
    PARTIALLY_DELIVERED — that includes a pass that was fully delivered and
    then balanced off by a return or a counting correction, which otherwise
    stayed stuck on a false DELIVERED and vanished from the delivery form.

    Otherwise the workflow states (RECEIVED / PROCESSING / READY_FOR_DELIVERY)
    are kept as-is: a pass that was never sent, only short-received, is not a
    partial delivery.
    """
    if current_status == "CANCELLED":
        return "CANCELLED"
    if balance["totals"]["outstanding_delivery_qty"] == 0:
        return "DELIVERED"
    if has_prior_send(balance):
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
                    "balance_adjustment_qty": b["balance_adjustment_qty"],
                }
            )
    return rows


def _sortable_stamp(value) -> Optional[float]:
    """Coerce a stored delivery stamp into a comparable float.

    Deliveries are written with a ``datetime`` but older hand-seeded rows can
    hold an ISO string or a plain date. Sorting a mixed set used to raise
    TypeError (datetime vs str), which turned the whole balance report into a
    500 for one legacy row. Anything unparseable sorts as "no date" and falls
    back to the creation stamp / id, which keeps the order total and stable.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def order_deliveries(docs: List[dict], target_id: str) -> List[dict]:
    """Deliveries in the order they were served, with ``target_id`` last.

    The report is a statement about one delivery in the running sequence, so it
    has to know which deliveries came before it. Ordering falls back through the
    delivery date, the creation stamp and finally the id so a sequence is
    always stable and total, even for hand-seeded rows.
    """
    def sort_key(doc: dict):
        stamp = _sortable_stamp(doc.get("delivery_date"))
        if stamp is None:
            stamp = _sortable_stamp(doc.get("created_at"))
        return (stamp is None, stamp or 0.0, str(doc.get("_id") or doc.get("id") or ""))

    ordered = sorted(docs or [], key=sort_key)
    target = str(target_id)
    if not any(str(d.get("_id") or d.get("id") or "") == target for d in ordered):
        # A target that is not in the list still has to be reportable, so the
        # caller can pass every delivery it knows about without pre-selecting.
        ordered.append({"id": target_id})
    return ordered



def compute_delivery_balance_report(
    gp_items: List[dict],
    delivery_doc: dict,
    deliveries_in_order: List[dict],
    returned_by_item: Optional[Dict[str, int]] = None,
    adjustment_docs: Optional[List[dict]] = None,
) -> dict:
    """Per-item running balance for ONE delivery — the delivery print report.

    Produces the four figures a signed delivery note has to show:

        previous_balance_qty   what was still outstanding BEFORE this delivery
        received_qty           total ever received on the gate pass for the item
        delivered_qty          what THIS delivery carried
        current_balance_qty    what was still outstanding AFTER this delivery

    The report is a HISTORICAL statement, so it is evaluated as of the end of
    this delivery: deliveries served after it, and corrections attached to
    them, are deliberately excluded. A note printed today must not change
    because the client was served again tomorrow.

    In the normal case the printed line reconciles exactly:

        current = previous - delivered + balance_adjustment

    ``current`` is never an independent calculation: it is the ENGINE's own
    outstanding for the item as of the end of this delivery
    (``received - delivered_upto + returned + adjustment_upto``, clamped at
    zero). Deriving it from a clamped ``previous`` instead produced a figure
    that disagreed with the gate-pass balance whenever the pre-delivery balance
    was already negative. ``reconciles`` reports that disagreement honestly
    instead of comparing the number against itself and always answering yes.

    Only items actually on the delivery are reported — a delivery note must not
    list items the client did not just receive.
    """
    returned_by_item = returned_by_item or {}
    adjustment_docs = adjustment_docs or []

    # Rows sharing a name+spec are summed, exactly as the gate-pass balance sums
    # them. Keying straight into this map let the last row win, so a pass with
    # two batches of the same item printed the SECOND batch's received quantity
    # and every running-balance figure on the note was wrong.
    received_by_key: Dict[str, dict] = {}
    for it in merge_gate_pass_items(gp_items):
        received_by_key[item_key(it.get("item_name", ""), it.get("specification"))] = it

    delivery_id = str(delivery_doc.get("id") or delivery_doc.get("_id") or "")
    ordered = list(deliveries_in_order or [delivery_doc])

    # Everything served up to AND INCLUDING this note. Anything after it is a
    # later delivery and has no bearing on what this note had to say.
    target_index = next(
        (
            i
            for i, d in enumerate(ordered)
            if str(d.get("_id") or d.get("id") or "") == delivery_id
        ),
        max(len(ordered) - 1, 0),
    )
    prefix = ordered[: target_index + 1]
    prefix_ids = {str(d.get("_id") or d.get("id") or "") for d in prefix} - {""}
    delivered_upto = compute_delivered_by_item(prefix)

    # A correction counts once its delivery has been served. A correction with
    # no delivery attached applies to the pass as a whole, so it counts on
    # every note.
    adj_upto_by_key: Dict[str, int] = {}
    adj_this_by_key: Dict[str, int] = {}
    for adj in adjustment_docs:
        if adj.get("status") in ("VOID", "CANCELLED"):
            continue
        owner = adj.get("delivery_id")
        if owner and str(owner) not in prefix_ids:
            continue
        key = item_key(adj.get("item_name", ""), adj.get("specification"))
        qty = int(adj.get("quantity", 0) or 0)
        adj_upto_by_key[key] = adj_upto_by_key.get(key, 0) + qty
        if owner and str(owner) == delivery_id:
            adj_this_by_key[key] = adj_this_by_key.get(key, 0) + qty
    adj_before_by_key = {
        k: adj_upto_by_key.get(k, 0) - adj_this_by_key.get(k, 0)
        for k in set(adj_upto_by_key) | set(adj_this_by_key)
    }

    rows: List[dict] = []
    totals = {
        "previous_balance_qty": 0,
        "received_qty": 0,
        "delivered_qty": 0,
        "balance_adjustment_qty": 0,
        "current_balance_qty": 0,
    }
    flags: List[str] = []

    for it in delivery_doc.get("items", []) or []:
        name = it.get("item_name", "")
        spec = it.get("specification")
        key = item_key(name, spec)
        gp_item = received_by_key.get(key, {})
        received = int(gp_item.get("received_qty", 0) or 0)
        returned = int(returned_by_item.get(key, 0) or 0)

        delivered_this = int(it.get("quantity", 0) or 0)
        delivered_before = int(delivered_upto.get(key, 0) or 0) - delivered_this
        adj_this = adj_this_by_key.get(key, 0)
        adj_before = adj_before_by_key.get(key, 0)

        raw_previous = received - delivered_before + returned + adj_before
        previous = max(0, raw_previous)
        # The engine's figure for this item once THIS delivery has been served.
        # This is the same expression the gate-pass balance uses, so the last
        # note on a pass always prints the pass's real outstanding figure.
        current = max(0, received - int(delivered_upto.get(key, 0) or 0) + returned
                      + int(adj_upto_by_key.get(key, 0) or 0))
        expected_current = max(0, previous - delivered_this + adj_this)

        row_flags: List[str] = []
        if raw_previous < 0:
            row_flags.append("OVER_DELIVERED_BEFORE_DELIVERY")
        elif delivered_this > previous:
            # This note hands over more than was outstanding. The create
            # endpoint caps quantity at the available balance, so this only
            # appears when quantities were edited after the fact.
            row_flags.append("DELIVERY_EXCEEDS_BALANCE")
        if adj_this > 0:
            row_flags.append("BALANCE_CREDITED")
        elif adj_this < 0:
            row_flags.append("BALANCE_DEBITED")
        if current != expected_current:
            row_flags.append("BALANCE_DOES_NOT_RECONCILE")
        flags.extend(row_flags)

        rows.append(
            {
                "item_key": key,
                "item_name": name,
                "specification": spec or "",
                "category": gp_item.get("category") or "",
                "previous_balance_qty": previous,
                "received_qty": received,
                "delivered_qty": delivered_this,
                "balance_adjustment_qty": adj_this,
                "current_balance_qty": current,
                "reconciles": current == expected_current,
                "flags": row_flags,
            }
        )
        totals["previous_balance_qty"] += previous
        totals["received_qty"] += received
        totals["delivered_qty"] += delivered_this
        totals["balance_adjustment_qty"] += adj_this
        totals["current_balance_qty"] += current

    return {
        "items": rows,
        "totals": totals,
        "flags": sorted(set(flags)),
    }


def compute_billable_on_received(
    gp_items: List[dict],
    billed_by_item: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Billable quantity per item based on the approved RECEIVED quantity.

    The billable event is the received quantity (business decision). Billable
    per item = received_qty - already billed quantity. Never negative.
    Items tagged ``rewashed`` are free re-washes and are NEVER billable — they
    are skipped entirely so they can never leak into a bill.
    """
    billed_by_item = billed_by_item or {}
    out: Dict[str, int] = {}
    for it in gp_items:
        if is_rewashed(it):
            continue
        key = item_key(it.get("item_name", ""), it.get("specification"))
        received = int(it.get("received_qty", 0) or 0)
        billed = int(billed_by_item.get(key, 0) or 0)
        out[key] = max(0, received - billed)
    return out


def compute_billable_received_by_name(
    gp_items_list: List[dict],
    billed_by_name: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Billable quantities for billing, aggregated by item NAME.

    Billing lines are item-name based (spec is not part of a bill line), so
    received quantities across specs of the same item are summed before the
    already-billed quantities are subtracted. Rewashed tags (free re-washes)
    are excluded so they are never billed.
    """
    billed_by_name = billed_by_name or {}
    received_by_name: Dict[str, int] = {}
    for it in gp_items_list:
        if is_rewashed(it):
            continue
        name = it.get("item_name", "")
        received_by_name[name] = received_by_name.get(name, 0) + int(it.get("received_qty", 0) or 0)
    return {
        name: max(0, received_by_name[name] - billed_by_name.get(name, 0))
        for name in received_by_name
    }


def detect_reconciliation_issues(
    balance: dict,
    status: str,
    legacy_marked: bool,
    billed_by_name: Optional[Dict[str, int]] = None,
) -> List[dict]:
    """Detect reconciliation issues for one gate pass (pure, DB-free).

    Categories:
      LEGACY_NOTE_CLOSURE        marked delivered by note with no records
      CLOSED_WITH_OUTSTANDING    closed but still has pieces to deliver
      OVER_DELIVERED             delivered more than received for an item
      SHORT_RECEIVED             received less than the waybill expected
      BILL_EXCEEDS_RECEIVED      billed more of an item than was received
    """
    issues: List[dict] = []
    if legacy_marked:
        issues.append(
            {
                "code": "LEGACY_NOTE_CLOSURE",
                "severity": "info",
                "detail": "Closed by the old mark-delivered note, not by recorded delivery records.",
            }
        )

    totals = balance.get("totals", {})
    if (
        (status in ("DELIVERED", "CLOSED"))
        and not legacy_marked
        and (totals.get("outstanding_delivery_qty") or 0) > 0
    ):
        issues.append(
            {
                "code": "CLOSED_WITH_OUTSTANDING",
                "severity": "high",
                "detail": f"Pass is {status} but {totals.get('outstanding_delivery_qty')} piece(s) are still not recorded as delivered.",
            }
        )

    billed_by_name = billed_by_name or {}
    received_by_name: Dict[str, int] = {}
    delivered_by_name: Dict[str, int] = {}
    expected_by_name: Dict[str, int] = {}
    for it in (balance.get("items") or {}).values():
        name = it.get("item_name", "")
        received_by_name[name] = received_by_name.get(name, 0) + it.get("received_qty", 0)
        delivered_by_name[name] = delivered_by_name.get(name, 0) + it.get("delivered_qty", 0)
        expected_by_name[name] = expected_by_name.get(name, 0) + it.get("expected_qty", 0)

    for name in sorted(set(received_by_name) | set(delivered_by_name) | set(expected_by_name)):
        received = received_by_name.get(name, 0)
        delivered = delivered_by_name.get(name, 0)
        expected = expected_by_name.get(name, 0)
        if delivered > received:
            issues.append(
                {
                    "code": "OVER_DELIVERED",
                    "severity": "high",
                    "detail": f"{name}: delivered {delivered} but only {received} received (both should never happen).",
                }
            )
        if received < expected:
            issues.append(
                {
                    "code": "SHORT_RECEIVED",
                    "severity": "info",
                    "detail": f"{name}: received {received} of the {expected} the waybill expected.",
                }
            )
        billed = billed_by_name.get(name, 0)
        if billed > received:
            issues.append(
                {
                    "code": "BILL_EXCEEDS_RECEIVED",
                    "severity": "high",
                    "detail": f"{name}: billed {billed} but only {received} received.",
                }
            )
    return issues


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
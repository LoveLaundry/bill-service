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

Delivery-time count reconciliation (reported, never billed):
  client_counted_qty        what the client's representative counted on handover
  discrepancy_qty           delivered - counted (>0 short, <0 over); 0 when the
                            client did not count. This is a QUANTITY balance
                            owed back to the client and is intentionally NOT
                            folded into outstanding_delivery_qty or any money
                            figure, so disputing a count can never silently
                            change an invoice.

ITEM-LEVEL TRACEABILITY
-----------------------
A delivery line records the gate pass its quantity came from
(``item.gate_pass_id``). A delivery may therefore draw from MANY gate passes,
and one gate pass may be fulfilled by MANY deliveries. Legacy delivery lines
that predate item-level attribution fall back to the delivery's own
``gate_pass_id``. Every per-gate-pass balance must be computed from lines
attributed to THAT gate pass only — never from a global sum, or quantities
would be duplicated between passes.

  source_gate_pass_ids(delivery)   all gate passes a delivery draws from
  compute_delivered_by_gate_pass   {gate_pass_id: {item_key: qty}}
  compute_availability            per-gate-pass deliverable quantities

The engine is pure (no database access). Callers pass decrypted documents.
"""
from typing import Dict, Iterable, List, Optional, Set


RETURN_ACTIONS_PENDING = ("RECEIVE_BACK", "RE_WASH")

# Workflow states that can never be turned back into "not yet received".
CANCELLED_STATUS = "CANCELLED"


def item_key(name: str, spec: Optional[str] = None) -> str:
    """Canonical per-item key. Always ``name||spec`` even when spec is empty."""
    return f"{name}||{spec or ''}"


def normalize_client(client_name: Optional[str]) -> str:
    """Canonical hotel/client identity used for equality comparisons.

    Matches the normalisation used by the blind search index
    (``crypto_helper.get_search_token``) so a delivery typed as
    ``"  Sunshine Hotel "`` is recognised as the same hotel as the gate pass
    stored as ``"Sunshine Hotel"``. Case- and whitespace-insensitive, which is
    what stops two spellings of one hotel being treated as two tenants.
    """
    return (client_name or "").strip().lower()


def same_client(a: Optional[str], b: Optional[str]) -> bool:
    """True when two client-name spellings denote the same hotel/client."""
    na, nb = normalize_client(a), normalize_client(b)
    return bool(na) and na == nb


def source_gate_pass_id(item: dict, delivery: dict) -> str:
    """Resolve the gate pass a single delivery line was drawn from.

    Item-level attribution wins. Legacy lines (stored before a delivery could
    span passes) fall back to the delivery's own ``gate_pass_id`` so historical
    records keep their original meaning.
    """
    item_gp = (item or {}).get("gate_pass_id")
    if item_gp:
        return str(item_gp)
    return str((delivery or {}).get("gate_pass_id") or "")


def source_gate_pass_ids(delivery: dict) -> List[str]:
    """Every gate pass a delivery draws from, in stable order.

    Includes the delivery's ``gate_pass_id`` so a legacy single-pass delivery
    still reports exactly one source.
    """
    out: List[str] = []
    seen: Set[str] = set()

    def _add(value) -> None:
        if not value:
            return
        key = str(value)
        if key not in seen:
            seen.add(key)
            out.append(key)

    _add((delivery or {}).get("gate_pass_id"))
    for raw in (delivery or {}).get("source_gate_pass_ids") or []:
        _add(raw)
    for it in (delivery or {}).get("items", []) or []:
        if isinstance(it, dict):
            _add(it.get("gate_pass_id"))
    return out


def compute_delivered_by_gate_pass(
    delivery_docs: Iterable[dict],
) -> Dict[str, Dict[str, int]]:
    """Delivered quantities grouped by the gate pass each line came from.

    This is the ONLY correct way to derive a per-gate-pass balance. Summing a
    flat item map and subtracting it from every pass double-counts whenever a
    delivery draws lines from more than one gate pass.
    """
    out: Dict[str, Dict[str, int]] = {}
    for dl in delivery_docs or []:
        if dl.get("status") == CANCELLED_STATUS:
            continue
        for it in dl.get("items", []) or []:
            if not isinstance(it, dict):
                continue
            gp_id = source_gate_pass_id(it, dl)
            if not gp_id:
                continue
            key = item_key(it.get("item_name", ""), it.get("specification"))
            bucket = out.setdefault(gp_id, {})
            bucket[key] = bucket.get(key, 0) + int(it.get("quantity", 0) or 0)
    return out


def gate_pass_received_map(gp_items: List[dict]) -> Dict[str, int]:
    """Received quantities for one gate pass, keyed canonically."""
    out: Dict[str, int] = {}
    for it in gp_items or []:
        key = item_key(it.get("item_name", ""), it.get("specification"))
        out[key] = out.get(key, 0) + int(it.get("received_qty", 0) or 0)
    return out


def compute_available(
    received_by_item: Dict[str, int],
    delivered_by_item: Optional[Dict[str, int]] = None,
    returned_by_item: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Quantity still deliverable per item, never negative.

    available = received - delivered + returned-but-not-yet-resent
    """
    delivered_by_item = delivered_by_item or {}
    returned_by_item = returned_by_item or {}
    out: Dict[str, int] = {}
    for key, received in received_by_item.items():
        value = int(received or 0) - int(delivered_by_item.get(key, 0) or 0)
        value += int(returned_by_item.get(key, 0) or 0)
        out[key] = max(0, value)
    return out


def is_rewashed(it: dict) -> bool:
    """True when an item is tagged as a free re-wash (never billed)."""
    return bool(it.get("rewashed"))


def flatten_name(key: str) -> str:
    """Recover item name from a canonical key."""
    return key.split("||", 1)[0]


def compute_delivered_by_item(delivery_docs: List[dict]) -> Dict[str, int]:
    """Sum delivered quantities across non-cancelled delivery documents.

    This is a HOTEL-WIDE view only (used for cross-pass totals such as
    "how many bedsheets did this hotel receive vs deliver"). For a per-gate-pass
    balance always use :func:`compute_delivered_by_gate_pass` so a line is only
    ever counted against the pass it actually came from.
    """
    out: Dict[str, int] = {}
    for dl in delivery_docs:
        if dl.get("status") == CANCELLED_STATUS:
            continue
        for it in dl.get("items", []):
            key = item_key(it.get("item_name", ""), it.get("specification"))
            out[key] = out.get(key, 0) + int(it.get("quantity", 0) or 0)
    return out


def compute_counted_by_item(delivery_docs: List[dict]) -> Dict[str, int]:
    """Sum the CLIENT-COUNTED quantities across non-cancelled deliveries.

    Only lines where the client actually counted (``client_counted_qty`` is a
    number) contribute. A delivery with no count contributes nothing, so a
    pass that was never reconciled does not look like a zero-count pass.
    """
    out: Dict[str, int] = {}
    for dl in delivery_docs:
        if dl.get("status") == "CANCELLED":
            continue
        for it in dl.get("items", []):
            counted = it.get("client_counted_qty")
            if counted is None:
                continue
            key = item_key(it.get("item_name", ""), it.get("specification"))
            out[key] = out.get(key, 0) + int(counted or 0)
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
    counted_by_item: Optional[Dict[str, int]] = None,
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
        # Delivery-time count reconciliation. Purely additive reporting: these
        # never feed outstanding_delivery_qty, billing, or any money figure.
        "client_counted_qty": 0,
        "discrepancy_qty": 0,
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

        counted = (counted_by_item or {}).get(key)
        has_count = counted is not None
        # Positive => we recorded more than the client counted (short-delivered,
        # owed back to the client). Negative => we recorded less than they
        # counted (over-delivered).
        discrepancy = (delivered - int(counted)) if has_count else 0

        effective_delivered = max(delivered, received) if marked_delivered else delivered
        outstanding = max(0, received - effective_delivered + returned)
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
        if has_count and discrepancy > 0:
            item_flags.append("DELIVERY_SHORT_COUNTED")
        elif has_count and discrepancy < 0:
            item_flags.append("DELIVERY_OVER_COUNTED")

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
            "client_counted_qty": int(counted) if has_count else None,
            "discrepancy_qty": discrepancy,
            "has_count": has_count,
            "flags": item_flags,
        }
        totals["expected_qty"] += expected
        totals["received_qty"] += received
        totals["delivered_qty"] += delivered
        totals["effective_delivered_qty"] += effective_delivered
        totals["returned_back_qty"] += returned
        totals["outstanding_delivery_qty"] += outstanding
        totals["not_received_qty"] += not_received
        totals["client_counted_qty"] += int(counted) if has_count else 0
        totals["discrepancy_qty"] += discrepancy

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
    gate_pass_id: Optional[str] = None,
) -> str:
    """Derive the real status after a quantity correction, INCLUDING movements.

    The approval of a gate-pass adjustment must never re-derive status from an
    empty movement set — that would downgrade a fully-delivered pass to
    PARTIALLY_DELIVERED/RECEIVED because recorded deliveries were ignored.

    ``gate_pass_id`` scopes the attribution: deliveries that also draw lines
    from other passes contribute only the lines belonging to this one.
    """
    if gate_pass_id is not None:
        delivered = compute_delivered_by_gate_pass(delivery_docs).get(str(gate_pass_id), {})
    else:
        delivered = compute_delivered_by_item(delivery_docs)
    returned = compute_returned_by_item(return_docs)
    balance = compute_gate_pass_balance(gp_items, delivered, returned)
    return derive_gate_pass_status(balance, current_status)


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

    # Spec-aware oversell check. This replaces a name-level check that summed
    # every specification of an item together, which both duplicated the report
    # (a "Pillow||King" oversell was flagged once here and again by the summed
    # loop) and could cancel a real violation out: an unrelated "Pillow||Queen"
    # line with slack would hide the King oversell entirely. The per-item flag
    # comes from the balance, which is keyed by name+spec, so this can neither
    # miss nor invent a violation.
    #
    # Nothing is lost by dropping the summed check: if the total delivered for
    # a name exceeds the total received, at least one specification of that name
    # must itself exceed, and this loop reports that one.
    for it in (balance.get("items") or {}).values():
        if "DELIVERED_EXCEEDS_RECEIVED" in (it.get("flags") or []):
            spec = it.get("specification") or ""
            label = f"{it.get('item_name')} ({spec})" if spec else it.get("item_name")
            issues.append(
                {
                    "code": "OVER_DELIVERED",
                    "severity": "high",
                    "detail": (
                        f"{label}: delivered {it.get('delivered_qty')} but only "
                        f"{it.get('received_qty')} received."
                    ),
                }
            )

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
        expected = expected_by_name.get(name, 0)
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


# ── Delivery validation ──────────────────────────────────────────────────────
class DeliveryValidationError(ValueError):
    """A delivery would break a quantity invariant (never a silent over-delivery).

    Carries a machine-readable ``code`` and an ``errors`` list so the API can
    return a precise, actionable 400/409 instead of a generic message.
    """

    def __init__(self, code: str, errors: List[dict]):
        self.code = code
        self.errors = errors
        detail = "; ".join(
            f"{e.get('item_name', 'item')}"
            + (f" ({e['specification']})" if e.get("specification") else "")
            + f": {e['detail']}"
            for e in errors
        )
        super().__init__(detail)


def find_oversell_violations(
    gate_pass_docs: List[dict],
    delivered_by_gp: Dict[str, Dict[str, int]],
    returned_by_gp: Optional[Dict[str, Dict[str, int]]] = None,
) -> List[dict]:
    """Any (gate pass, item) whose confirmed OUT exceeds its received IN.

    This is the system's central invariant — a hotel can never hand back more
    linen than it handed over — expressed as a pure, re-checkable predicate.
    It is deliberately derived from *stored state* rather than from the request
    being written, so it is equally valid as a pre-flight check, as the
    post-write concurrency guard, and as a reconciliation audit across every
    gate pass (where a single violation means the ledger is already wrong).

    Returns an empty list when the ledger is sound.
    """
    returned_by_gp = returned_by_gp or {}
    violations: List[dict] = []
    for gp in gate_pass_docs or []:
        gp_id = str(gp.get("id") or gp.get("_id") or gp.get("gate_pass_id") or "")
        if not gp_id or gp.get("status") == CANCELLED_STATUS:
            continue
        delivered = delivered_by_gp.get(gp_id, {}) or {}
        returned = returned_by_gp.get(gp_id, {}) or {}
        for gp_item in gp.get("items", []) or []:
            if not isinstance(gp_item, dict):
                continue
            key = item_key(gp_item.get("item_name"), gp_item.get("specification"))
            received_qty = int(gp_item.get("received_qty", 0) or 0)
            out_qty = int(delivered.get(key, 0) or 0) - int(returned.get(key, 0) or 0)
            if out_qty > received_qty:
                violations.append(
                    {
                        "code": "OVERSOLD_ITEM",
                        "gate_pass_id": gp_id,
                        "item_name": gp_item.get("item_name"),
                        "specification": gp_item.get("specification") or "",
                        "received_qty": received_qty,
                        "delivered_qty": int(delivered.get(key, 0) or 0),
                        "returned_qty": int(returned.get(key, 0) or 0),
                        "detail": (
                            f"{gp_item.get('item_name')}: {out_qty} out exceeds "
                            f"{received_qty} received on gate pass {gp_id}"
                        ),
                    }
                )
    return violations


def plan_delivery_lines(
    requested_lines: List[dict],
    gate_passes: Dict[str, dict],
    available_by_gp: Dict[str, Dict[str, int]],
    *,
    default_gate_pass_id: Optional[str] = None,
    exclude_delivery_id: Optional[str] = None,
) -> List[dict]:
    """Validate requested delivery lines and return the canonical line records.

    ``gate_passes``        {gate_pass_id: decrypted gate-pass document}
    ``available_by_gp``    {gate_pass_id: {item_key: still-deliverable qty}}
    ``default_gate_pass_id`` fallback source for lines that omit their own
    ``gate_pass_id`` (keeps older single-pass callers working unchanged).

    Every check the business rules demand happens here, once:
      * the line must name a gate pass that exists and is not cancelled;
      * the gate pass must belong to the same hotel as the delivery;
      * two lines may not target the same (gate pass, item) twice;
      * the summed quantity per (gate pass, item) must not exceed what that
        gate pass still has available — this is what stops
        ``received 23 / delivery 23 / delivery 23`` from ever becoming 46.

    Raises :class:`DeliveryValidationError` on the first violated rule group.
    """
    errors: List[dict] = []
    planned: List[dict] = []
    # (gate_pass_id, item_key) pairs already claimed by an accepted line, so a
    # duplicate or an over-split submission is rejected rather than merged.
    seen_buckets: Set[tuple] = set()

    for raw in requested_lines or []:
        item_name = (raw.get("item_name") or "").strip()
        spec = (raw.get("specification") or "") or None
        try:
            qty = int(raw.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        gp_id = str(raw.get("gate_pass_id") or default_gate_pass_id or "")

        if not item_name:
            errors.append(
                {"item_name": "", "specification": spec or "", "detail": "Item name is required."}
            )
            continue
        if qty <= 0:
            errors.append(
                {
                    "item_name": item_name,
                    "specification": spec or "",
                    "detail": f"Quantity must be at least 1 (received {qty}).",
                }
            )
            continue
        if not gp_id:
            errors.append(
                {
                    "item_name": item_name,
                    "specification": spec or "",
                    "detail": "No source gate pass — every delivered item must be traceable to the gate pass it came from.",
                }
            )
            continue

        gp = gate_passes.get(gp_id)
        if gp is None:
            errors.append(
                {
                    "item_name": item_name,
                    "specification": spec or "",
                    "gate_pass_id": gp_id,
                    "detail": "Source gate pass was not found.",
                }
            )
            continue
        if gp.get("status") == CANCELLED_STATUS:
            errors.append(
                {
                    "item_name": item_name,
                    "specification": spec or "",
                    "gate_pass_id": gp_id,
                    "detail": f"Source gate pass {gp.get('gate_pass_number', gp_id)} is cancelled.",
                }
            )
            continue

        declared_client = raw.get("_delivery_client_name")
        if declared_client is not None and not same_client(declared_client, gp.get("client_name")):
            errors.append(
                {
                    "item_name": item_name,
                    "specification": spec or "",
                    "gate_pass_id": gp_id,
                    "detail": (
                        f"Hotel mismatch: this delivery is for '{declared_client}' but gate pass "
                        f"{gp.get('gate_pass_number', gp_id)} belongs to '{gp.get('client_name')}'."
                    ),
                }
            )
            continue

        key = item_key(item_name, spec)
        bucket = (gp_id, key)

        # One line per (gate pass, item) per delivery. Rejecting duplicates
        # (rather than silently summing them) keeps the stored document
        # unambiguous: a later correction addresses exactly one line, and the
        # operator is told to consolidate rather than being handed a merged
        # line they never wrote.
        if bucket in seen_buckets:
            errors.append(
                {
                    "item_name": item_name,
                    "specification": spec or "",
                    "gate_pass_id": gp_id,
                    "gate_pass_number": gp.get("gate_pass_number"),
                    "detail": (
                        f"'{item_name}' is listed more than once for gate pass "
                        f"{gp.get('gate_pass_number', gp_id)}. Combine the rows "
                        "into a single line."
                    ),
                }
            )
            continue

        received = int((available_by_gp.get(gp_id) or {}).get(key, 0) or 0)
        if received <= 0:
            # Either the item was never received on this pass, or it is already
            # fully delivered. Distinguish so the operator knows which.
            received_total = gate_pass_received_map(gp.get("items", [])).get(key, 0)
            if received_total <= 0:
                detail = "was never received on this gate pass."
            else:
                detail = (
                    "has no remaining balance on this gate pass "
                    f"(received {received_total}, already fully delivered)."
                )
            errors.append(
                {
                    "item_name": item_name,
                    "specification": spec or "",
                    "gate_pass_id": gp_id,
                    "gate_pass_number": gp.get("gate_pass_number"),
                    "detail": detail,
                }
            )
            continue

        if qty > received:
            errors.append(
                {
                    "item_name": item_name,
                    "specification": spec or "",
                    "gate_pass_id": gp_id,
                    "gate_pass_number": gp.get("gate_pass_number"),
                    "requested": qty,
                    "available": max(0, received),
                    "detail": (
                        f"Only {max(0, received)} available on "
                        f"{gp.get('gate_pass_number', gp_id)} (received {received})."
                    ),
                }
            )
            continue

        seen_buckets.add(bucket)
        planned.append(
            {
                "item_name": item_name,
                "specification": spec,
                "gate_pass_id": gp_id,
                "quantity": qty,
                "client_counted_qty": raw.get("client_counted_qty"),
                "mismatch_reason": raw.get("mismatch_reason"),
                "mismatch_notes": raw.get("mismatch_notes"),
            }
        )

    if errors:
        raise DeliveryValidationError("DELIVERY_NOT_ALLOWED", errors)
    return planned


def build_availability(
    gate_pass_docs: List[dict],
    delivered_by_gp: Dict[str, Dict[str, int]],
    returned_by_gp: Optional[Dict[str, Dict[str, int]]] = None,
) -> List[dict]:
    """Per-gate-pass deliverable inventory, with full origin traceability.

    This is the single structure the delivery form, the gate-pass detail page
    and the dashboard all render, so an administrator never has to subtract
    quantities by hand. Each entry answers: which hotel, which receiving event,
    what was received, what has already been delivered, what is still owed
    back to us — and the exact remaining quantity.
    """
    returned_by_gp = returned_by_gp or {}
    out: List[dict] = []
    for gp in gate_pass_docs or []:
        if gp.get("status") == CANCELLED_STATUS:
            continue
        gp_id = str(gp.get("id") or gp.get("_id") or "")
        if not gp_id:
            continue
        delivered = delivered_by_gp.get(gp_id, {}) or {}
        returned = returned_by_gp.get(gp_id, {}) or {}
        balance = compute_gate_pass_balance(
            gp.get("items", []),
            delivered,
            returned,
            marked_delivered=bool(gp.get("marked_delivered")),
        )
        lines = []
        for row in balance["items"].values():
            available = row["outstanding_delivery_qty"]
            lines.append(
                {
                    "item_name": row["item_name"],
                    "specification": row["specification"],
                    "category": row["category"],
                    "expected_qty": row["expected_qty"],
                    "received_qty": row["received_qty"],
                    "delivered_qty": row["delivered_qty"],
                    "returned_qty": row["returned_back_qty"],
                    "available_qty": available,
                    "rewashed": row["rewashed"],
                    "flags": row["flags"],
                }
            )
        deliverable = [ln for ln in lines if ln["available_qty"] > 0]
        out.append(
            {
                "gate_pass_id": gp_id,
                "gate_pass_number": gp.get("gate_pass_number", ""),
                "client_name": (gp.get("client_name") or "").strip(),
                "receiving_date": gp.get("receiving_date"),
                "status": gp.get("status"),
                "derived_status": derive_gate_pass_status(balance, gp.get("status", "")),
                "total_received_qty": balance["totals"]["received_qty"],
                "total_delivered_qty": balance["totals"]["delivered_qty"],
                "total_available_qty": sum(ln["available_qty"] for ln in deliverable),
                "items": lines,
                "deliverable_items": deliverable,
            }
        )
    return out
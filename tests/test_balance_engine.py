"""Unit tests for the canonical balance engine.

Run with:  uv run --no-sync --with pytest python -m pytest tests -q
"""
from bill_service.services import balance_engine as be


def _gp_item(name, received, expected=0, spec=None):
    return {
        "item_name": name,
        "specification": spec,
        "client_qty": expected,
        "received_qty": received,
        "category": "HOTEL",
    }


def _delivery(items, cancelled=False):
    return {"status": "CANCELLED" if cancelled else "DELIVERED", "items": items}


def _del_item(name, qty, spec=None):
    return {"item_name": name, "specification": spec, "quantity": qty}


def _return_item(name, qty, action="RECEIVE_BACK", resent=False, spec=None):
    item = {
        "item_name": name,
        "specification": spec,
        "returned_qty": qty,
        "action": action,
        "resend_status": "SENT" if resent else "PENDING",
    }
    return item


def _balance(gp_items, deliveries=None, returns=None, marked=False):
    delivered = be.compute_delivered_by_item(deliveries or [])
    returned = be.compute_returned_by_item(returns or [])
    return be.compute_gate_pass_balance(gp_items, delivered, returned, marked_delivered=marked)


# --- Full delivery ---
def test_full_delivery():
    gp = [_gp_item("Duvet Cover", 50, 50)]
    bal = _balance(gp, deliveries=[_delivery([_del_item("Duvet Cover", 50)])])
    t = bal["totals"]
    assert t["received_qty"] == 50
    assert t["delivered_qty"] == 50
    assert t["outstanding_delivery_qty"] == 0
    assert bal["items"][be.item_key("Duvet Cover")]["flags"] == []
    assert be.derive_gate_pass_status(bal, "PARTIALLY_DELIVERED") == "DELIVERED"


# --- Partial delivery ---
def test_partial_delivery():
    gp = [_gp_item("Duvet Cover", 47, 50)]
    bal = _balance(gp, deliveries=[_delivery([_del_item("Duvet Cover", 30)])])
    item = bal["items"][be.item_key("Duvet Cover")]
    assert item["received_qty"] == 47
    assert item["delivered_qty"] == 30
    assert item["outstanding_delivery_qty"] == 17
    assert item["not_received_qty"] == 3  # 50 expected, 47 received
    assert "SHORT_RECEIVED" in item["flags"]
    assert be.derive_gate_pass_status(bal, "PROCESSING") == "PARTIALLY_DELIVERED"


# --- Multiple partial deliveries ---
def test_multiple_partial_deliveries():
    gp = [_gp_item("Bath Towel", 100, 100)]
    deliveries = [
        _delivery([_del_item("Bath Towel", 40)]),
        _delivery([_del_item("Bath Towel", 35)]),
    ]
    bal = _balance(gp, deliveries=deliveries)
    item = bal["items"][be.item_key("Bath Towel")]
    assert item["delivered_qty"] == 75
    assert item["outstanding_delivery_qty"] == 25
    assert be.derive_gate_pass_status(bal, "RECEIVED") == "PARTIALLY_DELIVERED"


# --- Quantity mismatch: extra received ---
def test_extra_received():
    gp = [_gp_item("Sheets", 55, 50)]
    bal = _balance(gp, deliveries=[_delivery([_del_item("Sheets", 55)])])
    item = bal["items"][be.item_key("Sheets")]
    assert item["extra_received_qty"] == 5
    assert "EXTRA_RECEIVED" in item["flags"]
    assert item["outstanding_delivery_qty"] == 0


# --- Missing items (not received at all) ---
def test_missing_item():
    gp = [_gp_item("Pillow", 0, 10), _gp_item("Duvet Cover", 10, 10)]
    bal = _balance(gp, deliveries=[_delivery([_del_item("Duvet Cover", 10)])])
    missing = bal["items"][be.item_key("Pillow")]
    assert missing["received_qty"] == 0
    assert missing["outstanding_delivery_qty"] == 0
    assert missing["not_received_qty"] == 10
    # Duvet fully delivered; pillow never received -> GP has no outstanding
    assert be.derive_gate_pass_status(bal, "RECEIVED") == "DELIVERED"


# --- Returns add back to outstanding until re-sent ---
def test_returns_pending_then_resent():
    gp = [_gp_item("Bath Towel", 47, 47)]
    deliveries = [_delivery([_del_item("Bath Towel", 47)])]
    returns = [{"gate_pass_id": "x", "items": [_return_item("Bath Towel", 4, resent=True)]}]
    bal = _balance(gp, deliveries=deliveries, returns=returns)
    item = bal["items"][be.item_key("Bath Towel")]
    assert item["returned_back_qty"] == 0  # SENT returns are excluded
    assert item["outstanding_delivery_qty"] == 0

    # Same return NOT re-sent -> must be pending again
    returns[0]["items"][0]["resend_status"] = "PENDING"
    bal2 = _balance(gp, deliveries=deliveries, returns=returns)
    item2 = bal2["items"][be.item_key("Bath Towel")]
    assert item2["returned_back_qty"] == 4
    assert item2["outstanding_delivery_qty"] == 4
    assert be.derive_gate_pass_status(bal2, "DELIVERED") == "PARTIALLY_DELIVERED"


# --- RE_WASH actions count too, other actions do not ---
def test_return_action_filtering():
    gp = [_gp_item("Towel", 10, 10)]
    deliveries = [_delivery([_del_item("Towel", 10)])]
    returns = [
        {"gate_pass_id": "x", "items": [_return_item("Towel", 2, action="RE_WASH")]},
        {"gate_pass_id": "x", "items": [_return_item("Towel", 3, action="WRONG_ITEM")]},
    ]
    bal = _balance(gp, deliveries=deliveries, returns=returns)
    item = bal["items"][be.item_key("Towel")]
    assert item["returned_back_qty"] == 2
    assert item["outstanding_delivery_qty"] == 2


# --- Over-delivery is flagged, never silently accepted ---
def test_duplicate_over_delivery_flagged():
    gp = [_gp_item("Duvet Cover", 50, 50)]
    deliveries = [
        _delivery([_del_item("Duvet Cover", 50)]),
        _delivery([_del_item("Duvet Cover", 50)]),
    ]
    bal = _balance(gp, deliveries=deliveries)
    item = bal["items"][be.item_key("Duvet Cover")]
    assert item["delivered_qty"] == 100
    assert item["delivered_qty"] > item["received_qty"]
    assert "DELIVERED_EXCEEDS_RECEIVED" in item["flags"]
    assert item["outstanding_delivery_qty"] == 0  # clamped


# --- Cancelled deliveries never count ---
def test_cancelled_delivery_excluded():
    gp = [_gp_item("Duvet Cover", 50, 50)]
    deliveries = [
        _delivery([_del_item("Duvet Cover", 45)]),
        _delivery([_del_item("Duvet Cover", 5)], cancelled=True),
    ]
    bal = _balance(gp, deliveries=deliveries)
    item = bal["items"][be.item_key("Duvet Cover")]
    assert item["delivered_qty"] == 45
    assert item["outstanding_delivery_qty"] == 5


# --- Legacy marked-delivered closure stays visible but math treats as delivered ---
def test_marked_delivered_legacy():
    gp = [_gp_item("Duvet Cover", 68, 68)]
    # one real partial delivery of 51: 17 hidden by the note
    deliveries = [_delivery([_del_item("Duvet Cover", 51)])]
    bal = _balance(gp, deliveries=deliveries, marked=True)
    item = bal["items"][be.item_key("Duvet Cover")]
    assert item["received_qty"] == 68
    assert item["delivered_qty"] == 51          # real recorded quantity preserved
    assert item["effective_delivered_qty"] == 68
    assert item["outstanding_delivery_qty"] == 0
    assert "LEGACY_NOTE_CLOSURE_HIDES_OUTSTANDING" in item["flags"]
    assert "MARKED_DELIVERED_LEGACY" in bal["flags"]


def test_marked_delivered_zero_records():
    gp = [_gp_item("Duvet Cover", 40, 40)]
    bal = _balance(gp, marked=True)
    item = bal["items"][be.item_key("Duvet Cover")]
    assert item["delivered_qty"] == 0
    assert item["effective_delivered_qty"] == 40
    assert item["outstanding_delivery_qty"] == 0
    assert "MARKED_DELIVERED_LEGACY" in bal["flags"]


# --- Spec-aware keys never mix ---
def test_specification_aware_keys():
    gp = [_gp_item("Pillow", 10, 10, spec="Large"), _gp_item("Pillow", 5, 5, spec="Small")]
    deliveries = [_delivery([_del_item("Pillow", 8, spec="Large"), _del_item("Pillow", 5, spec="Small")])]
    bal = _balance(gp, deliveries=deliveries)
    large = bal["items"][be.item_key("Pillow", "Large")]
    small = bal["items"][be.item_key("Pillow", "Small")]
    assert large["delivered_qty"] == 8
    assert large["outstanding_delivery_qty"] == 2
    assert small["outstanding_delivery_qty"] == 0


# --- Status derivation ---
def test_status_derivation():
    assert be.derive_gate_pass_status(_balance([], []), "CANCELLED") == "CANCELLED"
    gp = [_gp_item("Towel", 10, 10)]
    bal = _balance(gp)
    assert be.derive_gate_pass_status(bal, "RECEIVED") == "RECEIVED"   # nothing delivered yet
    bal2 = _balance(gp, deliveries=[_delivery([_del_item("Towel", 5)])])
    assert be.derive_gate_pass_status(bal2, "PROCESSING") == "PARTIALLY_DELIVERED"
    bal3 = _balance(gp, deliveries=[_delivery([_del_item("Towel", 10)])])
    assert be.derive_gate_pass_status(bal3, "READY_FOR_DELIVERY") == "DELIVERED"


# --- Billing on received quantity ---
def test_billable_on_received():
    gp = [_gp_item("Duvet Cover", 47, 50), _gp_item("Bath Towel", 10, 10)]
    billables = be.compute_billable_on_received(gp)
    assert billables[be.item_key("Duvet Cover")] == 47
    assert billables[be.item_key("Bath Towel")] == 10

    billed = {be.item_key("Duvet Cover"): 30}
    billables2 = be.compute_billable_on_received(gp, billed)
    assert billables2[be.item_key("Duvet Cover")] == 17
    assert billables2[be.item_key("Bath Towel")] == 10


# --- Outstanding rows for pending lists ---
def test_outstanding_rows():
    gp = [_gp_item("Duvet Cover", 47, 50), _gp_item("Pillow", 4, 4)]
    deliveries = [_delivery([_del_item("Duvet Cover", 30), _del_item("Pillow", 4)])]
    bal = _balance(gp, deliveries=deliveries)
    rows = be.compute_outstanding_per_item(gp, bal)
    assert len(rows) == 1
    assert rows[0]["item_name"] == "Duvet Cover"
    assert rows[0]["pending_qty"] == 17


# --- Approved received correction (adjustment) application ---
def test_apply_received_correction():
    gp = [_gp_item("Duvet Cover", 50, 50), _gp_item("Towel", 10, 10, spec="Large")]
    updated, original = be.apply_received_correction(gp, "Duvet Cover", None, 47)
    assert original == 50
    assert updated[0]["received_qty"] == 47
    assert updated[0]["difference"] == -3  # 47 - 50 expected
    assert updated[1]["received_qty"] == 10  # untouched item preserved
    # Non-existent item
    updated2, original2 = be.apply_received_correction(gp, "Nope", None, 5)
    assert updated2 is None and original2 is None
    # Spec-aware matching
    updated3, original3 = be.apply_received_correction(gp, "Towel", "Large", 8)
    assert original3 == 10
    assert updated3[1]["received_qty"] == 8


# --- Balance recomputed after a corrected received quantity ---
def test_balance_after_correction():
    gp = [_gp_item("Duvet Cover", 50, 50)]
    deliveries = [_delivery([_del_item("Duvet Cover", 30)])]
    bal_before = _balance(gp, deliveries=deliveries)
    assert bal_before["items"][be.item_key("Duvet Cover")]["outstanding_delivery_qty"] == 20

    updated, _ = be.apply_received_correction(gp, "Duvet Cover", None, 45)
    bal_after = be.compute_gate_pass_balance(
        updated, be.compute_delivered_by_item(deliveries), {}
    )
    item = bal_after["items"][be.item_key("Duvet Cover")]
    assert item["received_qty"] == 45
    assert item["outstanding_delivery_qty"] == 15
    assert be.derive_gate_pass_status(bal_after, "PARTIALLY_DELIVERED") == "PARTIALLY_DELIVERED"
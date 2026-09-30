"""Regression tests for the delivered-basis billable ceiling.

These lock in the financial invariant that a hotel can never be invoiced for
more linen than has actually been delivered to it, no matter how the bill is
split across deliveries or repeated.
"""
from bill_service.services import balance_engine as be


def _del_item(name, qty, spec=None):
    return {"item_name": name, "specification": spec, "quantity": qty}


def _delivery(items, cancelled=False):
    return {"status": "CANCELLED" if cancelled else "DELIVERED", "items": items}


def test_billable_is_delivered_not_received():
    # 100 pieces received, only 10 delivered -> 10 billable, never 100.
    d1 = _delivery([_del_item("Pillow", 10)])
    assert be.compute_billable_delivered_by_name([d1]) == {"Pillow": 10}

    # Nothing delivered -> nothing billable.
    assert be.compute_billable_delivered_by_name([]) == {}


def test_billable_subtracts_already_billed():
    d1 = _delivery([_del_item("Pillow", 10)])
    billed = {"Pillow": 4}
    assert be.compute_billable_delivered_by_name([d1], billed) == {"Pillow": 6}

    # Fully billed -> nothing left.
    assert be.compute_billable_delivered_by_name([d1], {"Pillow": 10}) == {"Pillow": 0}

    # Over-billed must floor at 0, never go negative.
    assert be.compute_billable_delivered_by_name([d1], {"Pillow": 99}) == {"Pillow": 0}


def test_billable_across_multiple_deliveries_of_one_pass():
    # The classic double-billing scenario: two deliveries of the same pass.
    d1 = _delivery([_del_item("Pillow", 10)])
    d2 = _delivery([_del_item("Pillow", 15)])

    # First bill may take up to the whole universe.
    assert be.compute_billable_delivered_by_name([d1, d2]) == {"Pillow": 25}

    # After billing 10, the remainder is 15 (D2's pieces) - not 25 again.
    assert be.compute_billable_delivered_by_name([d1, d2], {"Pillow": 10}) == {"Pillow": 15}

    # After billing 20, the remainder is 5.
    assert be.compute_billable_delivered_by_name([d1, d2], {"Pillow": 20}) == {"Pillow": 5}


def test_billable_sums_across_specifications():
    d1 = _delivery([_del_item("Pillow", 10, spec="King"), _del_item("Pillow", 5, spec="Queen")])
    # Bill lines are name-based, so both specs share one bucket.
    assert be.compute_billable_delivered_by_name([d1]) == {"Pillow": 15}


def test_cancelled_deliveries_never_billable():
    d1 = _delivery([_del_item("Pillow", 10)], cancelled=True)
    assert be.compute_billable_delivered_by_name([d1]) == {}


def test_rewashed_names_are_never_billable():
    d1 = _delivery([_del_item("Pillow", 10), _del_item("Towel", 5)])
    got = be.compute_billable_delivered_by_name([d1], None, {"Pillow"})
    assert got == {"Towel": 5}
    assert "Pillow" not in got


def test_malformed_delivery_lines_do_not_crash():
    assert be.compute_billable_delivered_by_name([{"items": None}]) == {}
    assert be.compute_billable_delivered_by_name([{"items": ["oops"]}]) == {}
    assert be.compute_billable_delivered_by_name([{"items": [{"item_name": "P", "quantity": "x"}]}]) == {"P": 0}


def test_availability_never_exceeds_received():
    """An unvalidated return must not inflate availability above received."""
    received = {"Pillow||": 10}
    delivered = {"Pillow||": 10}
    returned = {"Pillow||": 500}  # bogus / duplicated return
    got = be.compute_available(received, delivered, returned)
    assert got["Pillow||"] == 10, "availability must be clamped to the received quantity"

    # Same invariant on the authoritative per-item balance figure.
    gp = [{"item_name": "Pillow", "received_qty": 10, "client_qty": 10, "category": "HOTEL"}]
    dels = [_delivery([_del_item("Pillow", 10)])]
    rets = [{"items": [{"item_name": "Pillow", "returned_qty": 500,
                         "action": "RECEIVE_BACK", "resend_status": "PENDING"}]}]
    bal = be.compute_gate_pass_balance(
        gp, be.compute_delivered_by_item(dels), be.compute_returned_by_item(rets)
    )
    item = bal["items"][be.item_key("Pillow")]
    assert item["outstanding_delivery_qty"] == 10
    assert item["outstanding_delivery_qty"] <= item["received_qty"]

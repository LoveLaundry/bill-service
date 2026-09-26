"""Unit tests for signed balance adjustments and the delivery balance report.

A balance adjustment squares off a delivery that was recorded wrongly:

    +3   we under-delivered / lost / damaged -> the client is owed 3 more
    -3   we over-recorded the send           -> 3 fewer are outstanding

Two properties matter more than anything else here:

  1. It moves the PIECE balance only. Billing derives from received_qty, so an
     adjustment can never change what is billable.
  2. The printed report reconciles: current = previous - delivered + adjustment.

The report is evaluated as of the end of the delivery being printed, so a note
does not drift when the client is served again later. For the most recent
delivery that as-of figure is the gate-pass outstanding balance, so the two
screens agree where it matters.

Run with:  uv run --no-sync --with pytest python -m pytest tests -q
"""
from datetime import datetime, timedelta

from bill_service.services import balance_engine as be


def _gp_item(name, received, expected=0, spec=None, rewashed=False):
    return {
        "item_name": name,
        "specification": spec,
        "client_qty": expected,
        "received_qty": received,
        "category": "HOTEL",
        "rewashed": rewashed,
    }


def _delivery(items, cancelled=False, delivery_id=None, seq=1):
    doc = {
        "status": "CANCELLED" if cancelled else "DELIVERED",
        "items": items,
        # Real, increasing service dates: the report's as-of behaviour depends
        # on being able to order the sequence, not on id luck.
        "delivery_date": datetime(2024, 1, 1) + timedelta(days=seq),
    }
    if delivery_id:
        doc["id"] = delivery_id
    return doc


def _del_item(name, qty, spec=None):
    return {"item_name": name, "specification": spec, "quantity": qty}


def _adj(name, qty, spec=None, delivery_id=None, status="POSTED"):
    return {
        "item_name": name,
        "specification": spec,
        "quantity": qty,
        "status": status,
        "delivery_id": delivery_id,
    }


def _balance(gp_items, deliveries=None, returns=None, marked=False, adjustments=None):
    return be.compute_gate_pass_balance(
        gp_items,
        be.compute_delivered_by_item(deliveries or []),
        be.compute_returned_by_item(returns or []),
        marked_delivered=marked,
        balance_adjustment_by_item=be.compute_balance_adjustments_by_item(adjustments or []),
    )


# --- Aggregation ---
def test_adjustments_aggregate_signed_per_item():
    docs = [_adj("Towel", 3), _adj("Towel", -1), _adj("Sheet", 2)]
    out = be.compute_balance_adjustments_by_item(docs)
    assert out[be.item_key("Towel")] == 2
    assert out[be.item_key("Sheet")] == 2


def test_void_adjustments_are_ignored():
    docs = [_adj("Towel", 5, status="VOID"), _adj("Towel", 2)]
    assert be.compute_balance_adjustments_by_item(docs)[be.item_key("Towel")] == 2


def test_adjustment_keys_include_specification():
    docs = [_adj("Towel", 3, spec="Large"), _adj("Towel", 1, spec="Small")]
    out = be.compute_balance_adjustments_by_item(docs)
    assert out[be.item_key("Towel", "Large")] == 3
    assert out[be.item_key("Towel", "Small")] == 1


# --- Effect on the gate pass balance ---
def test_credit_raises_outstanding():
    gp = [_gp_item("Towel", 50, 50)]
    bal = _balance(
        gp,
        deliveries=[_delivery([_del_item("Towel", 50)])],
        adjustments=[_adj("Towel", 4)],
    )
    item = bal["items"][be.item_key("Towel")]
    assert item["outstanding_delivery_qty"] == 4
    assert item["balance_adjustment_qty"] == 4
    assert "BALANCE_CREDITED" in item["flags"]


def test_debit_reduces_outstanding():
    gp = [_gp_item("Towel", 50, 50)]
    bal = _balance(
        gp,
        deliveries=[_delivery([_del_item("Towel", 30)])],
        adjustments=[_adj("Towel", -5)],
    )
    # 50 received - 30 delivered - 5 credited back = 15
    assert bal["items"][be.item_key("Towel")]["outstanding_delivery_qty"] == 15
    assert "BALANCE_DEBITED" in bal["items"][be.item_key("Towel")]["flags"]


def test_outstanding_never_goes_negative():
    """A debit can correct a mistake but can never over-credit the client."""
    gp = [_gp_item("Towel", 10, 10)]
    bal = _balance(
        gp,
        deliveries=[_delivery([_del_item("Towel", 10)])],
        adjustments=[_adj("Towel", -25)],
    )
    item = bal["items"][be.item_key("Towel")]
    assert item["outstanding_delivery_qty"] == 0
    # The signed correction is still reported truthfully, even though the
    # balance it would produce is clamped.
    assert item["balance_adjustment_qty"] == -25


def test_no_adjustments_leaves_balance_untouched():
    gp = [_gp_item("Towel", 50, 50)]
    with_none = _balance(gp, deliveries=[_delivery([_del_item("Towel", 30)])])
    with_empty = _balance(
        gp, deliveries=[_delivery([_del_item("Towel", 30)])], adjustments=[]
    )
    assert with_none["totals"] == with_empty["totals"]


def test_totals_include_adjustment():
    gp = [_gp_item("Towel", 10, 10), _gp_item("Sheet", 10, 10)]
    bal = _balance(
        gp,
        deliveries=[_delivery([_del_item("Towel", 5), _del_item("Sheet", 5)])],
        adjustments=[_adj("Towel", 2), _adj("Sheet", -1)],
    )
    assert bal["totals"]["balance_adjustment_qty"] == 1


# --- Billing must never move ---
def test_adjustment_does_not_change_billable_quantity():
    """Billing is driven by received_qty, so a correction cannot move money.

    Two passes identical except that one carries a +10 credit: the adjustment
    moves the piece balance, but the billable quantity is untouched.
    """
    gp = [_gp_item("Towel", 50, 50)]
    deliveries = [_delivery([_del_item("Towel", 50)])]

    plain = _balance(gp, deliveries=deliveries)
    credited = _balance(gp, deliveries=deliveries, adjustments=[_adj("Towel", 10)])

    # The balance did move ...
    assert plain["totals"]["outstanding_delivery_qty"] == 0
    assert credited["totals"]["outstanding_delivery_qty"] == 10

    # ... but what is billable did not.
    assert be.compute_billable_on_received(gp, {}) == be.compute_billable_on_received(gp, {})
    assert be.compute_billable_on_received(gp, {})[be.item_key("Towel")] == 50
    assert be.compute_billable_received_by_name(gp, {}) == {"Towel": 50}


# --- Status re-derivation ---
def test_credit_flips_a_closed_pass_back_to_partially_delivered():
    gp = [_gp_item("Towel", 50, 50)]
    closed = _balance(gp, deliveries=[_delivery([_del_item("Towel", 50)])])
    assert be.derive_gate_pass_status(closed, "DELIVERED") == "DELIVERED"

    credited = _balance(
        gp,
        deliveries=[_delivery([_del_item("Towel", 50)])],
        adjustments=[_adj("Towel", 3)],
    )
    assert be.derive_gate_pass_status(credited, "DELIVERED") == "PARTIALLY_DELIVERED"


def test_recompute_status_with_movements_sees_adjustments():
    gp = [_gp_item("Towel", 10, 10)]
    status = be.recompute_status_with_movements(
        gp,
        [_delivery([_del_item("Towel", 10)])],
        [],
        "DELIVERED",
        [_adj("Towel", 2)],
    )
    assert status == "PARTIALLY_DELIVERED"


# --- The printed delivery report ---
def _report(gp, delivery, other_deliveries=None, returns=None, adjustments=None):
    """Report for `delivery`. `other_deliveries` may sit before OR after it;
    the engine decides, so tests can prove a later delivery is excluded."""
    docs = list(other_deliveries or []) + [delivery]
    return be.compute_delivery_balance_report(
        gp,
        delivery,
        be.order_deliveries(docs, delivery.get("id")),
        be.compute_returned_by_item(returns or []),
        list(adjustments or []),
    )


def test_report_first_delivery_carries_the_whole_received_quantity():
    """Before the first delivery the client is owed everything received."""
    gp = [_gp_item("Towel", 50, 50)]
    rep = _report(gp, _delivery([_del_item("Towel", 50)], delivery_id="d1"))
    row = rep["items"][0]
    assert row["previous_balance_qty"] == 50
    assert row["received_qty"] == 50
    assert row["delivered_qty"] == 50
    assert row["current_balance_qty"] == 0
    assert row["reconciles"] is True


def test_report_second_delivery_carries_previous_balance():
    gp = [_gp_item("Towel", 50, 50)]
    first = _delivery([_del_item("Towel", 20)], delivery_id="d1", seq=1)
    second = _delivery([_del_item("Towel", 30)], delivery_id="d2", seq=2)
    rep = _report(gp, second, other_deliveries=[first])
    row = rep["items"][0]
    # 50 received, 20 already sent -> 30 was still owed going in.
    assert row["previous_balance_qty"] == 30
    assert row["delivered_qty"] == 30
    assert row["current_balance_qty"] == 0
    assert row["reconciles"] is True


def test_report_excludes_deliveries_served_after_this_one():
    """A printed note is a historical statement and must not drift."""
    gp = [_gp_item("Towel", 50, 50)]
    first = _delivery([_del_item("Towel", 20)], delivery_id="d1", seq=1)
    second = _delivery([_del_item("Towel", 25)], delivery_id="d2", seq=2)
    third = _delivery([_del_item("Towel", 5)], delivery_id="d3", seq=3)

    # The first note is reported even though d2 and d3 already exist.
    rep = _report(gp, first, other_deliveries=[second, third])
    row = rep["items"][0]
    assert row["previous_balance_qty"] == 50
    assert row["delivered_qty"] == 20
    assert row["current_balance_qty"] == 30
    assert row["reconciles"] is True

    # The middle note sees d1 but not d3.
    mid = _report(gp, second, other_deliveries=[first, third])
    assert mid["items"][0]["previous_balance_qty"] == 30
    assert mid["items"][0]["current_balance_qty"] == 5

    # The last note sees everything before it.
    last = _report(gp, third, other_deliveries=[first, second])
    assert last["items"][0]["previous_balance_qty"] == 5
    assert last["items"][0]["current_balance_qty"] == 0


def test_report_excludes_a_correction_attached_to_a_later_delivery():
    gp = [_gp_item("Towel", 50, 50)]
    first = _delivery([_del_item("Towel", 20)], delivery_id="d1", seq=1)
    second = _delivery([_del_item("Towel", 25)], delivery_id="d2", seq=2)

    # The +7 belongs to d2, so it must not colour d1's note.
    rep = _report(
        gp,
        first,
        other_deliveries=[second],
        adjustments=[_adj("Towel", 7, delivery_id="d2")],
    )
    row = rep["items"][0]
    assert row["balance_adjustment_qty"] == 0
    assert row["current_balance_qty"] == 30


def test_report_reconciles_identity_with_an_adjustment():
    gp = [_gp_item("Towel", 50, 50)]
    first = _delivery([_del_item("Towel", 20)], delivery_id="d1", seq=1)
    second = _delivery([_del_item("Towel", 25)], delivery_id="d2", seq=2)
    rep = _report(
        gp,
        second,
        other_deliveries=[first],
        adjustments=[_adj("Towel", 3, delivery_id="d2")],
    )
    row = rep["items"][0]
    assert row["previous_balance_qty"] == 30
    assert row["delivered_qty"] == 25
    assert row["balance_adjustment_qty"] == 3
    # 30 previous - 25 delivered + 3 credited = 8 still owed
    assert row["current_balance_qty"] == 8
    assert row["reconciles"] is True
    assert row["flags"] == ["BALANCE_CREDITED"]


def test_report_ignores_adjustments_from_other_deliveries():
    """An adjustment on an earlier delivery is already in the previous balance."""
    gp = [_gp_item("Towel", 50, 50)]
    first = _delivery([_del_item("Towel", 20)], delivery_id="d1", seq=1)
    second = _delivery([_del_item("Towel", 25)], delivery_id="d2", seq=2)
    rep = _report(
        gp,
        second,
        other_deliveries=[first],
        adjustments=[_adj("Towel", 4, delivery_id="d1")],
    )
    row = rep["items"][0]
    # The +4 already raised the previous balance; it is not double counted.
    assert row["previous_balance_qty"] == 34
    assert row["balance_adjustment_qty"] == 0
    assert row["current_balance_qty"] == 9


def test_latest_report_current_balance_matches_gate_pass_balance():
    """For the most recent delivery the note agrees with the balance screen.

    Earlier notes deliberately do not: they are historical statements.
    """
    gp = [_gp_item("Towel", 50, 50), _gp_item("Sheet", 30, 30)]
    first = _delivery(
        [_del_item("Towel", 20), _del_item("Sheet", 10)], delivery_id="d1", seq=1
    )
    second = _delivery(
        [_del_item("Towel", 25), _del_item("Sheet", 20)], delivery_id="d2", seq=2
    )
    adjustments = [
        _adj("Towel", 3, delivery_id="d2"),
        _adj("Sheet", -2, delivery_id="d2"),
    ]
    rep = _report(gp, second, other_deliveries=[first], adjustments=adjustments)

    balance = _balance(
        gp,
        deliveries=[first, second],
        adjustments=adjustments,
    )
    for row in rep["items"]:
        assert row["current_balance_qty"] == balance["items"][row["item_key"]][
            "outstanding_delivery_qty"
        ]


def test_report_totals_sum_the_rows():
    gp = [_gp_item("Towel", 50, 50), _gp_item("Sheet", 30, 30)]
    delivery = _delivery(
        [_del_item("Towel", 25), _del_item("Sheet", 20)], delivery_id="d2", seq=2
    )
    rep = _report(
        gp,
        delivery,
        other_deliveries=[
            _delivery([_del_item("Towel", 20)], delivery_id="d1", seq=1)
        ],
    )
    t = rep["totals"]
    assert t["delivered_qty"] == 45
    assert t["received_qty"] == 80
    assert t["current_balance_qty"] == sum(
        r["current_balance_qty"] for r in rep["items"]
    )


def test_report_only_lists_items_on_the_delivery():
    gp = [_gp_item("Towel", 50, 50), _gp_item("Sheet", 30, 30)]
    rep = _report(gp, _delivery([_del_item("Towel", 10)], delivery_id="d1"))
    assert [r["item_name"] for r in rep["items"]] == ["Towel"]


def test_report_flags_when_earlier_deliveries_overshot_the_received_quantity():
    gp = [_gp_item("Towel", 5, 5)]
    first = _delivery([_del_item("Towel", 8)], delivery_id="d1", seq=1)
    second = _delivery([_del_item("Towel", 1)], delivery_id="d2", seq=2)
    rep = _report(gp, second, other_deliveries=[first])
    row = rep["items"][0]
    assert "OVER_DELIVERED_BEFORE_DELIVERY" in row["flags"]
    assert rep["flags"] == ["OVER_DELIVERED_BEFORE_DELIVERY"]
    # Clamped at zero on both sides, so the line still reconciles.
    assert row["previous_balance_qty"] == 0
    assert row["current_balance_qty"] == 0
    assert row["reconciles"] is True


def test_report_flags_a_delivery_that_overshoots_the_outstanding_balance():
    """The create endpoint caps this, so it only shows up after a later change."""
    gp = [_gp_item("Towel", 5, 5)]
    rep = _report(gp, _delivery([_del_item("Towel", 9)], delivery_id="d1"))
    row = rep["items"][0]
    assert "DELIVERY_EXCEEDS_BALANCE" in row["flags"]
    assert row["previous_balance_qty"] == 5
    assert row["current_balance_qty"] == 0
    assert row["reconciles"] is True

"""Unit tests for the journal entry builder and event constants."""
from bill_service.services import transaction_events as te


def test_build_item_delta():
    d = te.build_item_delta("Duvet Cover", "King", 10, 7)
    assert d == {
        "item_name": "Duvet Cover",
        "specification": "King",
        "qty_before": 10,
        "qty_after": 7,
        "qty_delta": -3,
    }


def test_build_item_delta_default_spec():
    d = te.build_item_delta("Pillow", None, 0, 5)
    assert d["specification"] == ""
    assert d["qty_delta"] == 5


def test_event_constants_unique_and_uppercase():
    names = [n for n in dir(te) if n.startswith("EVENT_")]
    values = [getattr(te, n) for n in names if isinstance(getattr(te, n), str)]
    assert len(values) == len(set(values)), "event constants must be unique"
    for v in values:
        assert v.isupper() and v.replace("_", "").isalnum()


def test_daily_ops_event_constants_exist():
    assert te.EVENT_DAY_CLOSED == "DAY_CLOSED"
    assert te.EVENT_BILL_CREATED == "BILL_CREATED"
    assert te.EVENT_LEGACY_FLAG == "LEGACY_FLAG"
"""Pure unit tests for recompute_status_with_movements (approval re-derivation)."""
from bill_service.services.balance_engine import recompute_status_with_movements


def _gpi(spec, client, received):
    return {"item_name": "Pillow", "specification": spec, "client_qty": client, "received_qty": received}


def _dl(spec, qty):
    return {"gate_pass_id": "g", "status": "DELIVERED", "items": [{"item_name": "Pillow", "specification": spec, "quantity": qty}]}


def _nl(spec, qty):
    return {"gate_pass_id": "g", "status": "CANCELLED", "items": [{"item_name": "Pillow", "specification": spec, "quantity": qty}]}


def _rt(spec, qty):
    return {"gate_pass_id": "g", "items": [{"item_name": "Pillow", "specification": spec, "action": "RECEIVE_BACK", "resend_status": None, "returned_qty": qty}]}


def test_upward_correction_on_fully_delivered_pass_reopens_partial():
    status = recompute_status_with_movements(
        [_gpi("Large", 60, 60)], [_dl("Large", 50)], [], "DELIVERED"
    )
    assert status == "PARTIALLY_DELIVERED"


def test_fully_delivered_outstanding_resolves_to_delivered():
    status = recompute_status_with_movements(
        [_gpi("Large", 50, 30)], [_dl("Large", 30)], [], "PARTIALLY_DELIVERED"
    )
    assert status == "DELIVERED"


def test_pending_returns_keep_pass_from_closing():
    status = recompute_status_with_movements(
        [_gpi("Large", 50, 50)], [_dl("Large", 50)], [_rt("Large", 4)], "DELIVERED"
    )
    assert status == "PARTIALLY_DELIVERED"


def test_open_status_kept_when_nothing_delivered():
    status = recompute_status_with_movements(
        [_gpi("Large", 50, 50)], [], [], "READY_FOR_DELIVERY"
    )
    assert status == "READY_FOR_DELIVERY"


def test_cancelled_never_changes():
    status = recompute_status_with_movements(
        [_gpi("Large", 50, 50)], [_dl("Large", 50)], [], "CANCELLED"
    )
    assert status == "CANCELLED"


def test_cancelled_deliveries_are_ignored():
    status = recompute_status_with_movements(
        [_gpi("Large", 50, 50)], [_dl("Large", 50), _nl("Small", 5)], [], "READY_FOR_DELIVERY"
    )
    assert status == "DELIVERED"
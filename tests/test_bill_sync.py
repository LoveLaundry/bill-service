"""Tests for automatic bill re-sync after gate-pass item corrections.

The critical regression covered here: bills are item-name based (the biller
aggregates specification variants via ``compute_billable_received_by_name``),
so re-syncing must match received quantities by item NAME — never by
``name::spec``, which silently zeroed every spec'd bill line.
"""
from datetime import datetime, timezone

from bill_service.crypto_helper import decrypt_dict, encrypt_dict
from bill_service.services import bill_sync

BILL_SENSITIVE = bill_sync.BILL_SENSITIVE_FIELDS


def _now():
    return datetime.now(timezone.utc)


def _bill_doc(*, gate_pass_id: str, items: list[dict], payment_status: str = "PENDING", **extra) -> dict:
    total_amount = round(sum(i["line_total"] for i in items), 2)
    grand_total = round(total_amount - 0 + 0 + 0 + 0, 2)
    doc = {
        "gate_pass_id": gate_pass_id,
        "client_name": "Test Client",
        "items": items,
        "total_amount": total_amount,
        "total_quantity": float(sum(i["quantity"] for i in items)),
        "grand_total": grand_total,
        "outstanding_amount": grand_total,
        "discounts": 0,
        "transport_fee": 0,
        "taxes": 0,
        "additional_charges": 0,
        "paid_amount": 0,
        "notes": "",
        "payment_status": payment_status,
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc.update(extra)
    return doc


async def _store_bill(mocked_db, doc: dict):
    encrypted = encrypt_dict(doc, BILL_SENSITIVE)
    res = await mocked_db["bills_collection"].insert_one(encrypted)
    return str(res.inserted_id)


async def _read_bill(mocked_db, bill_id: str) -> dict:
    from bson import ObjectId

    raw = await mocked_db["bills_collection"].find_one({"_id": ObjectId(bill_id)})
    return decrypt_dict(raw, BILL_SENSITIVE)


def _gp_item(**kw) -> dict:
    base = {"item_name": "Pillow", "specification": None, "category": "Bed", "received_qty": 0}
    base.update(kw)
    return base


async def test_specd_items_do_not_zero_name_based_bill_lines(mocked_db):
    """Two specification variants of 'Pillow' must NOT zero a 'Pillow' bill line."""
    gp_id = "gp-1"
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=gp_id,
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 10, "line_total": 1000}],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id,
        [_gp_item(specification="Large", received_qty=5), _gp_item(specification="Small", received_qty=5)],
        user_id="u-1",
    )

    assert outcomes[0]["action"] == "no_change"
    after = await _read_bill(mocked_db, bill_id)
    assert after["items"][0]["quantity"] == 10
    assert after["grand_total"] == 1000


async def test_downward_correction_clamps_and_recomputes_totals(mocked_db):
    gp_id = "gp-2"
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=gp_id,
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 30, "line_total": 3000}],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id,
        [_gp_item(specification="Large", received_qty=22)],
        user_id="u-1",
        reason="damaged goods",
    )

    assert outcomes[0]["action"] == "adjusted"
    after = await _read_bill(mocked_db, bill_id)
    line = after["items"][0]
    assert line["quantity"] == 22
    assert line["line_total"] == 2200
    assert after["grand_total"] == 2200
    assert after["outstanding_amount"] == 2200
    assert "re-synced to corrected quantities" in after["notes"]


async def test_bill_never_increased_automatically(mocked_db):
    """A deliberately partial bill (20) must not be inflated to received (50)."""
    gp_id = "gp-3"
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=gp_id,
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 10, "quantity": 20, "line_total": 200}],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id, [_gp_item(specification="Large", received_qty=50)], user_id="u-1"
    )

    assert outcomes[0]["action"] == "no_change"
    after = await _read_bill(mocked_db, bill_id)
    assert after["items"][0]["quantity"] == 20


async def test_removed_item_line_is_dropped(mocked_db):
    gp_id = "gp-4"
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=gp_id,
            items=[
                {"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 10, "line_total": 1000},
                {"item_name": "Towel", "category": "Bath", "unit_price": 50, "quantity": 5, "line_total": 250},
            ],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id, [_gp_item(specification="Large", received_qty=10)], user_id="u-1"
    )

    assert outcomes[0]["action"] == "adjusted"
    after = await _read_bill(mocked_db, bill_id)
    assert [i["item_name"] for i in after["items"]] == ["Pillow"]
    assert after["grand_total"] == 1000
    assert after["total_quantity"] == 10


async def test_newly_received_item_reported_not_auto_added(mocked_db):
    gp_id = "gp-5"
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=gp_id,
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 5, "line_total": 500}],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id,
        [_gp_item(specification="Large", received_qty=5), _gp_item(item_name="Towel", specification="XL", category="Bath", received_qty=8)],
        user_id="u-1",
    )

    assert outcomes[0]["action"] == "adjusted"
    after = await _read_bill(mocked_db, bill_id)
    assert [i["item_name"] for i in after["items"]] == ["Pillow"]
    assert "NOT billed here" in after["notes"]
    assert "Towel" in after["notes"]


async def test_paid_bill_is_flagged_never_rewritten(mocked_db):
    gp_id = "gp-6"
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=gp_id,
            payment_status="PAID",
            paid_amount=3000,
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 30, "line_total": 3000}],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id, [_gp_item(specification="Large", received_qty=22)], user_id="u-1"
    )

    assert outcomes[0]["action"] == "flagged"
    after = await _read_bill(mocked_db, bill_id)
    assert after["items"][0]["quantity"] == 30  # untouched
    assert after["grand_total"] == 3000
    assert "NOT re-written" in after["notes"]


async def test_cancelled_bill_skipped_never_rewritten(mocked_db):
    """CANCELLED bills are excluded from the match entirely."""
    gp_id = "gp-7"
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=gp_id,
            payment_status="CANCELLED",
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 30, "line_total": 3000}],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id, [_gp_item(specification="Large", received_qty=22)], user_id="u-1"
    )

    assert outcomes == []
    after = await _read_bill(mocked_db, bill_id)
    assert after["items"][0]["quantity"] == 30


async def test_delivery_reference_alone_matches_bill(mocked_db):
    """A bill linked only through delivery_ids (no gate_pass_id) is still matched."""
    gp_id = "gp-8"
    other_gp_id = "gp-other"
    delivery_result = await mocked_db["deliveries_collection"].insert_one(
        {
            "gate_pass_id": gp_id,
            "status": "DELIVERED",
            "items": [{"item_name": "Pillow", "specification": "Large", "quantity": 30}],
        }
    )
    delivery_id = str(delivery_result.inserted_id)
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=other_gp_id,
            delivery_ids=[delivery_id],
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 30, "line_total": 3000}],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id, [_gp_item(specification="Large", received_qty=22)], user_id="u-1"
    )

    assert outcomes and outcomes[0]["action"] == "adjusted"
    after = await _read_bill(mocked_db, bill_id)
    assert after["items"][0]["quantity"] == 22


async def test_overpaid_bill_after_correction_reports_negative_outstanding_note(mocked_db):
    gp_id = "gp-9"
    bill_id = await _store_bill(
        mocked_db,
        _bill_doc(
            gate_pass_id=gp_id,
            paid_amount=1000,
            items=[{"item_name": "Pillow", "category": "Bed", "unit_price": 100, "quantity": 10, "line_total": 1000}],
        ),
    )

    outcomes = await bill_sync.sync_bills_to_gate_pass(
        gp_id, [_gp_item(specification="Large", received_qty=5)], user_id="u-1"
    )

    assert outcomes[0]["action"] == "adjusted"
    after = await _read_bill(mocked_db, bill_id)
    assert after["outstanding_amount"] == 0
    assert "Overpaid by LKR 500" in after["notes"]
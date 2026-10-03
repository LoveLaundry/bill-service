"""Tests for the Monthly Operations feature.

Covers the DRAFT gate-pass/delivery flow, empty-day and negative-quantity
guards, idempotent day confirmation, delivery source modes, and month-length
handling (including leap years). The earlier phase tests already cover the
balance engine and idempotency primitives; these focus on the monthly router.

NOTE: collection assertions go through the ``mocked_db`` fixture. A ``from
main_db import X`` at module import time binds the original (real-client)
collections before the session fixture patches them, so reads must use the
patched client dictionary instead.
"""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from bill_service.crypto_helper import decrypt_dict, encrypt_dict
from bill_service.models import MonthlyDayConfirm, MonthlyQuantitiesUpdate
from bill_service.routers import monthly as m
from bill_service.routers.deliveries import activate_delivery
from bill_service.routers.gatepasses import update_gate_pass_status
from bill_service.services import balance_engine as be

GP_SENSITIVE = ["client_name", "items", "notes"]

CLIENT = "Hotel Amagi"
Y = 2026
MO = 9


def _req(headers=None):
    hdrs = [(k.lower().encode(), f"{v}".encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/monthly/receiving/Hotel/2026/9/1/confirm",
            "headers": hdrs,
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )


def user(name="Alice"):
    return {"auth_id": f"u-{name}", "user_name": name, "role": "ADMIN"}


async def put(kind, quantities, client=CLIENT, year=Y, month=MO, day=1):
    return await m.update_day_quantities(
        kind, client, year, month, day, MonthlyQuantitiesUpdate(quantities=quantities),
        current_user=user(),
    )


async def confirm(kind, *, client=CLIENT, year=Y, month=MO, day=1, body=None, key=None):
    payload = body or MonthlyDayConfirm()
    return await m.confirm_monthly_day(
        kind, client, year, month, day, payload, current_user=user(), request=_req(
            {"X-Idempotency-Key": key} if key else None
        ),
    )


async def gp_docs(mocked_db):
    docs = []
    for doc in await mocked_db["gatepasses_collection"].find({}).to_list(None):
        d = decrypt_dict(doc, GP_SENSITIVE)
        d["id"] = str(d["_id"])
        docs.append(d)
    return docs


# ---------------------------------------------------------------------------
# Balance engine DRAFT semantics
# ---------------------------------------------------------------------------
def test_draft_delivery_excluded_from_balance():
    deliveries = [
        {"status": "DRAFT", "items": [{"item_name": "Towel", "specification": None, "quantity": 10}]},
        {"status": "DELIVERED", "items": [{"item_name": "Towel", "specification": None, "quantity": 5}]},
        {"status": "CANCELLED", "items": [{"item_name": "Towel", "specification": None, "quantity": 9}]},
    ]
    delivered = be.compute_delivered_by_item(deliveries)
    assert delivered.get("Towel||") == 5


def test_draft_gate_pass_status_preserved():
    gp = [{"item_name": "Duvet Cover", "specification": None, "client_qty": 10, "received_qty": 10}]
    bal = be.compute_gate_pass_balance(gp, {}, {})
    assert be.derive_gate_pass_status(bal, "DRAFT") == "DRAFT"
    assert be.derive_gate_pass_status(bal, "CANCELLED") == "CANCELLED"
    # 10 received, 0 delivered -> open workflow status is preserved, not derived.
    assert be.derive_gate_pass_status(bal, "RECEIVED") == "RECEIVED"

    # Once fully delivered the derived statuses still resolve correctly.
    full = be.compute_gate_pass_balance(
        gp,
        be.compute_delivered_by_item(
            [{"status": "DELIVERED", "items": [{"item_name": "Duvet Cover", "specification": None, "quantity": 10}]}]
        ),
    )
    assert be.derive_gate_pass_status(full, "PARTIALLY_DELIVERED") == "DELIVERED"


# ---------------------------------------------------------------------------
# Quantities guardrails
# ---------------------------------------------------------------------------
async def test_negative_quantity_rejected():
    with pytest.raises(HTTPException) as exc:
        await put("receiving", {"Towel||": -5})
    assert exc.value.status_code == 400
    assert "Negative" in exc.value.detail


async def test_day_outside_month_rejected():
    # Sep 2026 has 30 days; day 31 is invalid.
    with pytest.raises(HTTPException) as exc:
        await put("receiving", {"Towel||": 5}, day=31)
    assert exc.value.status_code == 400


async def test_leap_year_february_allows_29():
    # 2024 is a leap year -> day 29 valid.
    await put("receiving", {"Towel||": 5}, year=2024, month=2, day=29)
    with pytest.raises(HTTPException):
        await put("receiving", {"Towel||": 5}, year=2026, month=2, day=29)


# ---------------------------------------------------------------------------
# Receiving confirm -> DRAFT gate pass
# ---------------------------------------------------------------------------
async def test_receiving_confirm_creates_draft_gate_pass(mocked_db):
    await put("receiving", {"Duvet Cover||": 50, "Towel||": 30})
    result = await confirm("receiving", body=MonthlyDayConfirm(source_mode=None))

    day_state = result["days"]["1"]
    assert day_state["status"] == "CONFIRMED"
    assert day_state["total_qty"] == 80
    assert len(day_state["gate_pass_ids"]) == 1
    assert day_state["delivery_ids"] == []

    gps = await gp_docs(mocked_db)
    assert len(gps) == 1
    gp = gps[0]
    assert gp["status"] == "DRAFT"
    assert gp["client_name"] == CLIENT
    assert gp["gate_pass_number"].startswith("GP-20260901-")
    assert gp["origin"]["kind"] == "monthly"
    assert len(gp["items"]) == 2


async def test_receiving_confirm_idempotent_with_key(mocked_db):
    await put("receiving", {"Towel||": 12})
    key = "mm-key-1"
    first = await confirm("receiving", body=MonthlyDayConfirm(), key=key)
    second = await confirm("receiving", body=MonthlyDayConfirm(), key=key)

    assert first["id"] == second["id"]
    assert len(await gp_docs(mocked_db)) == 1  # no duplicate gate pass


async def test_confirm_empty_day_rejected():
    # A month doc must exist (from another day) so the day entry being missing
    # triggers the per-day guard rather than a missing month doc.
    await put("receiving", {"Towel||": 5}, day=2)
    await put("receiving", {"Towel||": 0}, day=1)  # clears day 1 entirely
    with pytest.raises(HTTPException) as exc:
        await confirm("receiving", day=1)
    assert exc.value.status_code == 400
    assert "Enter quantities" in exc.value.detail


async def test_confirm_without_any_entries_rejected():
    with pytest.raises(HTTPException) as exc:
        await confirm("receiving")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Delivery confirm -> DRAFT delivery + activation
# ---------------------------------------------------------------------------
async def _seed_receiving_day(client=CLIENT, year=Y, month=MO, day=1, qty=50):
    await put("receiving", {"Duvet Cover||": qty}, client=client, year=year, month=month, day=day)
    await confirm("receiving", client=client, year=year, month=month, day=day)


async def test_delivery_same_day_and_activation(mocked_db):
    await _seed_receiving_day(day=1)

    await put("delivery", {"Duvet Cover||": 10}, day=1)
    result = await confirm("delivery", body=MonthlyDayConfirm(source_mode="same_day"), day=1)
    day_state = result["days"]["1"]
    assert day_state["status"] == "CONFIRMED"
    assert len(day_state["delivery_ids"]) == 1

    delivery_docs = []
    for doc in await mocked_db["deliveries_collection"].find({}).to_list(None):
        d = decrypt_dict(doc, ["client_name", "items", "notes"])
        d["id"] = str(d["_id"])
        delivery_docs.append(d)
    assert len(delivery_docs) == 1
    dl = delivery_docs[0]
    assert dl["status"] == "DRAFT"
    assert dl["items"][0]["quantity"] == 10

    # Activate the gate pass first (DRAFT -> RECEIVED), then the delivery.
    gp = (await gp_docs(mocked_db))[0]
    await update_gate_pass_status(gp["id"], "RECEIVED", current_user=user())
    activated = await activate_delivery(dl["id"], current_user=user())
    assert activated["status"] == "DELIVERED"

    gps = await gp_docs(mocked_db)
    assert gps[0]["status"] == "PARTIALLY_DELIVERED"


async def test_delivery_exceeds_available_rejected():
    await _seed_receiving_day(day=1, qty=50)
    await put("delivery", {"Duvet Cover||": 60}, day=1)
    with pytest.raises(HTTPException) as exc:
        await confirm("delivery", body=MonthlyDayConfirm(source_mode="same_day"), day=1)
    assert exc.value.status_code == 409


async def test_delivery_same_day_without_receiving_rejected():
    await put("delivery", {"Duvet Cover||": 10}, day=1)
    with pytest.raises(HTTPException) as exc:
        await confirm("delivery", body=MonthlyDayConfirm(source_mode="same_day"), day=1)
    assert exc.value.status_code == 400


async def test_activation_attribution_is_per_gate_pass(mocked_db):
    """Stock delivered from another pass must not shrink this pass's balance."""
    # Two separate receiving days -> two gate passes of 50 each.
    await _seed_receiving_day(day=1, qty=50)
    await _seed_receiving_day(day=2, qty=50)
    gps = await gp_docs(mocked_db)
    first, second = gps[0], gps[1]

    # Drain the FIRST pass completely with a real (activated) delivery.
    real = {
        "gate_pass_id": first["id"],
        "client_name": CLIENT,
        "delivery_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "delivered_by": "Alice",
        "received_by": "Bob",
        "items": [{"item_name": "Duvet Cover", "specification": None, "quantity": 50}],
        "status": "DELIVERED",
        "created_at": datetime.now(timezone.utc),
    }
    res = await mocked_db["deliveries_collection"].insert_one(
        encrypt_dict(real, ["client_name", "items", "notes"])
    )

    # A monthly DRAFT delivery against the SECOND pass must still see 50
    # available: the first pass's 50 must not be subtracted from it.
    draft = {
        "gate_pass_id": second["id"],
        "client_name": CLIENT,
        "delivery_date": datetime(2026, 9, 2, tzinfo=timezone.utc),
        "delivered_by": "Alice",
        "received_by": "Bob",
        "items": [{"item_name": "Duvet Cover", "specification": None, "quantity": 50}],
        "status": "DRAFT",
        "created_at": datetime.now(timezone.utc),
    }
    draft_res = await mocked_db["deliveries_collection"].insert_one(
        encrypt_dict(draft, ["client_name", "items", "notes"])
    )

    activated = await activate_delivery(str(draft_res.inserted_id), current_user=user())
    assert activated["status"] == "DELIVERED"

    # And the engine-level view agrees: per-pass attribution is not flat.
    per_pass = be.compute_delivered_by_gate_pass([real, activated])
    assert per_pass[first["id"]]["Duvet Cover||"] == 50
    assert per_pass[second["id"]]["Duvet Cover||"] == 50


async def test_delivery_manual_mode(mocked_db):
    await _seed_receiving_day(day=1, qty=50)
    gp = (await gp_docs(mocked_db))[0]
    await put("delivery", {"Duvet Cover||": 8}, day=1)
    manual = MonthlyDayConfirm(
        source_mode="manual",
        sources=[{"gate_pass_id": gp["id"], "items": [{"item_name": "Duvet Cover", "quantity": 8}]}],
    )
    result = await confirm("delivery", body=manual, day=1)
    assert result["days"]["1"]["status"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# Rewash confirm
# ---------------------------------------------------------------------------
async def test_rewash_confirm_records_not_chargeable_by_default(mocked_db):
    await put("rewash", {"Duvet Cover||": 4})
    result = await confirm("rewash", day=1)
    assert result["days"]["1"]["status"] == "CONFIRMED"
    assert len(result["days"]["1"]["rewash_ids"]) == 1

    rewash_docs = []
    for doc in await mocked_db["rewashes_collection"].find({}).to_list(None):
        rewash_docs.append(decrypt_dict(doc, ["client_name", "notes"]))
    assert len(rewash_docs) == 1
    rw = rewash_docs[0]
    assert rw["status"] == "RECORDED"
    assert rw["chargeable"] is False
    assert rw["client_name"] == CLIENT
    assert rw["items"][0]["quantity"] == 4
    assert rw["rewash_number"].startswith("RW-20260901-")


# ---------------------------------------------------------------------------
# Monthly matrix read shape
# ---------------------------------------------------------------------------
async def test_matrix_returns_rows_days_cells():
    await put("receiving", {"Duvet Cover||": 50}, day=1)
    await put("receiving", {"Towel||": 10}, day=2)
    matrix = await m.get_monthly_matrix(
        "receiving", CLIENT, Y, MO, current_user=user()
    )
    assert matrix["kind"] == "receiving"
    assert matrix["month_length"] == 30
    assert len(matrix["days"]) == 30
    assert matrix["days"][0]["status"] == "DRAFT"
    assert matrix["days"][1]["total_qty"] == 10
    assert "Duvet Cover||" in matrix["cells"]
    assert matrix["cells"]["Duvet Cover||"]["1"] == 50
    assert matrix["cells"]["Towel||"]["2"] == 10


async def test_matrix_invalid_kind_rejected():
    with pytest.raises(HTTPException) as exc:
        await m.get_monthly_matrix("orders", CLIENT, Y, MO, current_user=user())
    assert exc.value.status_code == 400
"""Verification and overdue-cycle queries used by the session scanner.

A verification read must not move `last_scanned_date`; otherwise the scanner
would make every fake-tag check refresh the very linen the overdue list is
looking for. The overdue query itself must stay tenant-scoped.
"""
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from bill_service.routers import linens as lin
from conftest import LAUNDRY_A, LAUNDRY_B, tenant_user


def _linen(laundry_id, linen_id, *, status="IN_STOCK", last_scanned=None, **extra):
    return {
        "laundry_id": laundry_id,
        "linen_id": linen_id,
        "category": "BEDSHEET",
        "item_type": "SINGLE",
        "client_name": "Hotel X",
        "status": status,
        "condition": "GOOD",
        "location": None,
        "wash_count": 0,
        "last_scanned_date": last_scanned,
        "created_at": None,
        "updated_at": None,
        **extra,
    }


async def _insert(mocked_db, doc):
    doc["_id"] = ObjectId()
    await mocked_db["linens_collection"].insert_one(doc)
    return doc


async def test_verification_does_not_change_last_scanned_date(mocked_db):
    # mongomock stores whole seconds and strips tz info; use a stable value.
    old = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=30)
    doc = await _insert(mocked_db, _linen(LAUNDRY_A, "LL-VERIFY1", last_scanned=old))

    result = await lin.verify_linen_code("ll-verify1", current_user=tenant_user())

    assert result["linen_id"] == "LL-VERIFY1"
    assert result["id"] == str(doc["_id"])
    returned = result["last_scanned_date"]
    if returned.tzinfo is None:
        returned = returned.replace(tzinfo=timezone.utc)
    assert returned == old
    stored = await mocked_db["linens_collection"].find_one({"linen_id": "LL-VERIFY1"})
    assert stored["last_scanned_date"].replace(tzinfo=timezone.utc) == old


async def test_verification_cannot_read_another_laundrys_tag(mocked_db):
    await _insert(mocked_db, _linen(LAUNDRY_B, "LL-FOREIGN"))

    with pytest.raises(Exception) as err:
        await lin.verify_linen_code("LL-FOREIGN", current_user=tenant_user())
    assert getattr(err.value, "status_code", None) == 404


async def test_missing_in_cycle_uses_the_window_and_stays_in_tenant(mocked_db):
    now = datetime.now(timezone.utc)
    await _insert(mocked_db, _linen(LAUNDRY_A, "LL-OLD0001", last_scanned=now - timedelta(days=12)))
    await _insert(mocked_db, _linen(LAUNDRY_A, "LL-FRESH01", last_scanned=now - timedelta(days=1)))
    await _insert(mocked_db, _linen(LAUNDRY_A, "LL-NEVER01", last_scanned=None))
    await _insert(mocked_db, _linen(LAUNDRY_A, "LL-GONE0001", status="MISSING", last_scanned=now))
    await _insert(mocked_db, _linen(LAUNDRY_A, "LL-RETIRE1", status="RETIRED", last_scanned=now - timedelta(days=99)))
    await _insert(mocked_db, _linen(LAUNDRY_B, "LL-THEIRS1", last_scanned=now - timedelta(days=99)))

    result = await lin.missing_in_cycle(days=7, current_user=tenant_user())

    codes = {item["linen_id"] for item in result["items"]}
    assert result["days"] == 7
    assert result["total"] == 3
    assert codes == {"LL-OLD0001", "LL-NEVER01", "LL-GONE0001"}


async def test_missing_in_cycle_rejects_an_absurd_window(mocked_db):
    with pytest.raises(Exception):
        await lin.missing_in_cycle(days=0, current_user=tenant_user())

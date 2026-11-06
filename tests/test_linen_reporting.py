"""QR lifecycle reporting: public track endpoint + laundry scan report."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from bill_service.app_time import today as lkt_today
from bill_service.database import main_db
from bill_service.routers.linens import public_linen_track, scan_activity_report


from conftest import tenant_user


def user():
    return tenant_user("tester", "acme")


async def _seed_linen(linen_id="LL-TEST01", **over):
    doc = {
        "linen_id": linen_id,
        "category": "BEDSHEET",
        "item_type": "Double",
        "status": "WASHING",
        "condition": "GOOD",
        "wash_count": 3,
        "laundry_id": "acme",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    doc.update(over)
    await main_db.linens_collection.insert_one(doc)


async def test_public_track_returns_sanitized_status(mocked_db):
    await _seed_linen()
    out = await public_linen_track("ll-test01")  # case-insensitive
    assert out["linen_id"] == "LL-TEST01"
    assert out["status"] == "WASHING"
    assert out["wash_count"] == 3
    # Sanitized: no client_name, location, notes or staff fields leak.
    assert "client_name" not in out
    assert "location" not in out
    assert "notes" not in out


async def test_public_track_unknown_code_404(mocked_db):
    with pytest.raises(HTTPException) as exc:
        await public_linen_track("LL-NOPE99")
    assert exc.value.status_code == 404


async def test_public_track_ambiguous_code_409(mocked_db):
    await _seed_linen("LL-DUP001", laundry_id="acme")
    await _seed_linen("LL-DUP001", laundry_id="other")
    with pytest.raises(HTTPException) as exc:
        await public_linen_track("LL-DUP001")
    assert exc.value.status_code == 409


async def test_scan_activity_aggregates_events(mocked_db):
    await main_db.linen_events_collection.insert_many([
        {
            "laundry_id": "acme",
            "linen_id": "LL-A",
            "action": "receive",
            "to_status": "AT_LAUNDRY",
            "timestamp": datetime.now(timezone.utc),
        },
        {
            "laundry_id": "acme",
            "linen_id": "LL-B",
            "action": "wash",
            "to_status": "WASHING",
            "timestamp": datetime.now(timezone.utc),
        },
        {
            "laundry_id": "other",  # foreign tenant must not leak
            "linen_id": "LL-X",
            "action": "receive",
            "to_status": "AT_LAUNDRY",
            "timestamp": datetime.now(timezone.utc),
        },
    ])
    out = await scan_activity_report(
        date_from=None, date_to=None, limit=10, current_user=user()
    )
    assert out["total_scans"] == 2
    assert out["by_action"]["receive"] == 1
    assert out["by_status"]["AT_LAUNDRY"] == 1
    assert out["by_status"]["WASHING"] == 1
    assert len(out["recent"]) == 2


async def test_scan_activity_date_filter(mocked_db):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    await main_db.linen_events_collection.insert_many([
        {"laundry_id": "acme", "linen_id": "LL-OLD", "action": "receive",
         "to_status": "AT_LAUNDRY", "timestamp": old},
        {"laundry_id": "acme", "linen_id": "LL-NEW", "action": "receive",
         "to_status": "AT_LAUNDRY", "timestamp": datetime.now(timezone.utc)},
    ])
    today = lkt_today().isoformat()
    out = await scan_activity_report(
        date_from=today, date_to=today, limit=10, current_user=user()
    )
    assert out["total_scans"] == 1
    assert out["recent"][0]["linen_id"] == "LL-NEW"

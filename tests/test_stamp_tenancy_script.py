"""The backfill CLI must be safe: report-only by default, never overwrite.

``scripts/stamp_tenancy.py`` is the operator's migration tool for the
transitional tenancy rule (unstamped documents belong to ``lovelaundry``). A
wrong write here is unrecoverable production data loss, so its guardrails are
tested against a mongomock database rather than trusted.
"""
import sys
from pathlib import Path

from mongomock_motor import AsyncMongoMockClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.stamp_tenancy import (  # noqa: E402
    LOVE_LAUNDRY_ID,
    TENANT_COLLECTIONS,
    select_collections,
    stamp_database,
)


def _db():
    return AsyncMongoMockClient()["main_db_test"]


async def test_dry_run_reports_without_writing():
    db = _db()
    await db["bills"].insert_one({"n": 1})

    counts = await stamp_database(db)

    assert counts == {"bills": 1}
    assert "laundry_id" not in await db["bills"].find_one({"n": 1})


async def test_apply_stamps_only_missing_and_never_overwrites():
    db = _db()
    await db["bills"].insert_many([
        {"n": 1},
        {"n": 2, "laundry_id": None},
        {"n": 3, "laundry_id": "other"},
    ])

    counts = await stamp_database(db, apply=True)

    assert counts == {"bills": 2}
    assert (await db["bills"].find_one({"n": 3}))["laundry_id"] == "other"
    assert (await db["bills"].find_one({"n": 1}))["laundry_id"] == LOVE_LAUNDRY_ID


async def test_idempotent_second_run_stamps_nothing():
    db = _db()
    await db["gatepasses"].insert_one({"n": 1})

    await stamp_database(db, apply=True)

    assert await stamp_database(db, apply=True) == {"gatepasses": 0}


async def test_flags_cannot_reach_infrastructure_collections():
    db = _db()
    await db["idempotency_keys"].insert_one({"key": "k1"})
    await db["sync_queue"].insert_one({"status": "PENDING"})

    for kwargs in ({"apply": True}, {"apply": True, "only": {"sync_queue", "idempotency_keys"}}):
        assert await stamp_database(db, **kwargs) == {}
    assert "laundry_id" not in await db["idempotency_keys"].find_one({"key": "k1"})
    assert "laundry_id" not in await db["sync_queue"].find_one({"status": "PENDING"})

    # A --only request cannot widen past the tenant allow-list either.
    assert select_collections(["sync_queue", "hotel_codes"], only={"sync_queue"}) == []
    assert set(TENANT_COLLECTIONS) >= {"bills", "gatepasses", "deliveries", "shop_bills"}

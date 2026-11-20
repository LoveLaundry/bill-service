"""Tag lookup and bulk scan against the multi-tenant registry.

`test_linen_live_clock.py` already proves the clock advances. This file covers
what that suite does not: the lookup query itself, which was unscoped and could
return another laundry's document for a shared code, and bulk scan, which
aborted the entire batch when a single code was unknown.
"""
import pytest
from bson import ObjectId

from bill_service.models import LinenScanAction
from bill_service.routers import linens as lin

from conftest import LAUNDRY_A, LAUNDRY_B, tenant_user


def _linen(laundry_id, linen_id, *, status="IN_STOCK", **extra):
    return {
        "laundry_id": laundry_id,
        "linen_id": linen_id,
        "category": "Bed linen",
        "item_type": "King sheet",
        "client_name": "Cinnamon Grand",
        "status": status,
        "condition": "NEW",
        "location": None,
        "wash_count": 0,
        "last_scanned_date": None,
        "created_at": None,
        "updated_at": None,
        **extra,
    }


async def _insert(mocked_db, doc):
    doc["_id"] = ObjectId()
    await mocked_db["linens_collection"].insert_one(doc)
    return doc


# ── lookup is scoped to the caller's laundry ─────────────────────────────────


async def test_lookup_finds_own_tag_when_another_laundry_holds_the_code(mocked_db):
    """A duplicate code elsewhere must not shadow this laundry's own tag.

    The unscoped query returned whichever document Mongo matched first, so the
    rightful laundry could get a 404 for its own tag. Inserting the foreign copy
    first reproduces that ordering.
    """
    await _insert(mocked_db, _linen(LAUNDRY_B, "LL-TEST01"))
    mine = await _insert(mocked_db, _linen(LAUNDRY_A, "LL-TEST01"))

    result = await lin.get_linen_by_code("ll-test01", current_user=tenant_user())

    assert result["linen_id"] == "LL-TEST01"
    assert result["laundry_id"] == LAUNDRY_A
    assert result["id"] == str(mine["_id"])


async def test_lookup_of_a_foreign_only_code_is_a_404(mocked_db):
    await _insert(mocked_db, _linen(LAUNDRY_B, "LL-OTHER1"))

    with pytest.raises(Exception) as err:
        await lin.get_linen_by_code("LL-OTHER1", current_user=tenant_user())
    assert getattr(err.value, "status_code", None) == 404


async def test_lookup_does_not_move_the_live_clock(mocked_db):
    """Reading a tag changes no tracked field, so it must not wake other screens."""
    await _insert(mocked_db, _linen(LAUNDRY_A, "LL-QUIET1"))

    await lin.get_linen_by_code("LL-QUIET1", current_user=tenant_user())

    assert (await lin.get_live_clock(current_user=tenant_user()))["clock"] == 0


# ── scan ─────────────────────────────────────────────────────────────────────


async def test_complete_wash_increments_the_wash_count(mocked_db):
    doc = await _insert(
        mocked_db, _linen(LAUNDRY_A, "LL-WASH02", status="WASHING", wash_count=7)
    )

    result = await lin.scan_linen(
        str(doc["_id"]),
        LinenScanAction(action="complete_wash"),
        current_user=tenant_user(),
    )

    assert result["status"] == "DRYING"
    assert result["wash_count"] == 8


async def test_scan_cannot_reach_another_laundrys_piece(mocked_db):
    foreign = await _insert(mocked_db, _linen(LAUNDRY_B, "LL-FOREIGN"))

    with pytest.raises(Exception) as err:
        await lin.scan_linen(
            str(foreign["_id"]),
            LinenScanAction(action="collect"),
            current_user=tenant_user(),
        )
    assert getattr(err.value, "status_code", None) == 404

    untouched = await mocked_db["linens_collection"].find_one({"linen_id": "LL-FOREIGN"})
    assert untouched["status"] == "IN_STOCK"


# ── bulk scan ────────────────────────────────────────────────────────────────


async def test_bulk_scan_reports_unknown_codes_without_losing_the_batch(mocked_db):
    """One bad code used to abort the request and discard every valid one."""
    await _insert(mocked_db, _linen(LAUNDRY_A, "LL-GOOD01"))

    result = await lin.bulk_scan_linens(
        ["LL-GOOD01", "LL-NOPE99"],
        "collect",
        location="Van 1",
        user=None,
        current_user=tenant_user(),
    )

    assert result["total_processed"] == 1
    assert result["total_errors"] == 1
    assert result["errors"][0]["linen_id"] == "LL-NOPE99"

    stored = await mocked_db["linens_collection"].find_one({"linen_id": "LL-GOOD01"})
    assert stored["status"] == "COLLECTED"
    assert stored["location"] == "Van 1"


async def test_bulk_scan_ignores_another_laundrys_code(mocked_db):
    await _insert(mocked_db, _linen(LAUNDRY_B, "LL-THEIRS"))

    result = await lin.bulk_scan_linens(
        ["LL-THEIRS"], "ready", location=None, user=None, current_user=tenant_user()
    )

    assert result["total_processed"] == 0
    assert result["total_errors"] == 1

    untouched = await mocked_db["linens_collection"].find_one({"linen_id": "LL-THEIRS"})
    assert untouched["status"] == "IN_STOCK"

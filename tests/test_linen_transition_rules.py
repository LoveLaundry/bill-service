"""Admin-defined linen transition legality.

The default matrix blocks re-collecting a linen that is already collected, but
still lets it move forward to washing. Admins can flip to warn mode, and the
per-tenant matrix is read from Company Settings in the shared MAIN database.
"""
import pytest
from fastapi import HTTPException

from bill_service.database import main_db
from bill_service.models import LinenCreate, LinenScanAction
from bill_service.routers import linens
from bill_service.services import linen_transition_policy
from conftest import LAUNDRY_A, tenant_user


def ua():
    return tenant_user("rules-a", LAUNDRY_A, "ADMIN")


async def _linen(status="IN_STOCK"):
    made = await linens.create_linen(
        LinenCreate(category="Towel", item_type="Bath Towel", client_name="Hotel A"), ua()
    )
    if status != "IN_STOCK":
        await linens.linens_collection.update_one(
            {"_id": linens.ObjectId(made["id"])}, {"$set": {"status": status}}
        )
    return made


async def _set_policy(rules, mode="block"):
    # Import here (not at module scope): the session fixture swaps the DB handles
    # after module import, so a module-level handle would be stale/live.
    from bill_service.database.main_db import company_settings_collection

    await company_settings_collection.insert_one(
        {
            "key": "main",
            "laundry_id": LAUNDRY_A,
            "linen_transition_rules": {"violation_mode": mode, "rules": rules},
        }
    )


async def test_default_blocks_double_collect(mocked_db):
    made = await _linen("COLLECTED")
    with pytest.raises(HTTPException) as exc:
        await linens.scan_linen(made["id"], LinenScanAction(action="collect"), ua())
    assert exc.value.status_code == 409
    assert "COLLECTED" in str(exc.value.detail)


async def test_default_allows_next_cycle_step(mocked_db):
    made = await _linen("COLLECTED")
    scanned = await linens.scan_linen(made["id"], LinenScanAction(action="receive"), ua())
    assert scanned["status"] == "AT_LAUNDRY"


async def test_warn_mode_records_scan_with_warning(mocked_db):
    await _set_policy(
        [{"from_status": "COLLECTED", "to_statuses": ["AT_LAUNDRY"]}], mode="warn"
    )
    made = await _linen("COLLECTED")
    scanned = await linens.scan_linen(made["id"], LinenScanAction(action="collect"), ua())
    assert scanned["status"] == "COLLECTED"
    assert "transition_warning" in scanned

    events = await linens.linen_events_collection.find({"linen_id": made["linen_id"]}).to_list(10)
    assert events[0]["notes"] and "Cannot move" in events[0]["notes"]


async def test_bulk_scan_respects_rules(mocked_db):
    made = await _linen("COLLECTED")
    result = await linens.bulk_scan_linens([made["linen_id"]], "collect", current_user=ua())
    assert result["total_processed"] == 0
    assert result["total_errors"] == 1
    assert "COLLECTED" in result["errors"][0]["error"]


async def test_bulk_scan_warn_mode_reports_warning(mocked_db):
    await _set_policy(
        [{"from_status": "COLLECTED", "to_statuses": ["AT_LAUNDRY"]}], mode="warn"
    )
    made = await _linen("COLLECTED")
    result = await linens.bulk_scan_linens([made["linen_id"]], "collect", current_user=ua())
    assert result["total_processed"] == 1
    assert result["warnings"][0]["linen_id"] == made["linen_id"]


async def test_transition_endpoint_publishes_policy(mocked_db):
    await _set_policy([{"from_status": "IN_STOCK", "to_statuses": ["COLLECTED"]}], mode="warn")
    result = await linens.list_transitions(ua())
    assert result.violation_mode == "warn"
    assert result.transition_rules[0].from_status == "IN_STOCK"


async def test_tenant_policy_is_isolated(mocked_db):
    await _set_policy([{"from_status": "WASHING", "to_statuses": ["DRYING"]}])
    policy = await linen_transition_policy.load_transition_policy(ua())
    assert policy["rules"][0]["from_status"] == "WASHING"

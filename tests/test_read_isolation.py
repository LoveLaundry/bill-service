"""Read-path isolation: aggregates, timelines, and secondary resources.

Every list/report/timeline endpoint must return only the caller's laundry.
Seeds one record per laundry, then asserts each side sees exactly its own.
"""
from datetime import datetime, timezone

from bill_service.crypto_helper import encrypt_dict
from bill_service.routers.bills import SENSITIVE_FIELDS as BILL_SENSITIVE
from bill_service.models import DispatchCreate, LinenCreate, LinenScanAction, LoyaltyAdjust
from bill_service.routers import (
    dashboard,
    day_close,
    day_money,
    dispatch,
    events,
    linens,
    loyalty,
    notifications,
    reconciliation,
)
from bill_service.routers.bills import GATEPASS_SENSITIVE_FIELDS
from conftest import LAUNDRY_A, LAUNDRY_B, tenant_user

USER_A = None
USER_B = None


def ua():
    global USER_A
    if USER_A is None:
        USER_A = tenant_user("reader-a", LAUNDRY_A, "ADMIN")
    return USER_A


def ub():
    global USER_B
    if USER_B is None:
        USER_B = tenant_user("reader-b", LAUNDRY_B, "ADMIN")
    return USER_B


def _now():
    return datetime.now(timezone.utc)


async def _seed_gp(mocked_db, number, laundry_id, client="Hotel X", status="RECEIVED"):
    doc = {
        "laundry_id": laundry_id,
        "gate_pass_number": number,
        "client_name": client,
        "status": status,
        "receiving_date": _now().isoformat(),
        "items": [{
            "item_name": "Pillow", "specification": None, "category": "Bed",
            "client_qty": 4, "received_qty": 4, "difference": 0, "rewashed": False,
        }],
        "notes": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    res = await mocked_db["gatepasses_collection"].insert_one(
        encrypt_dict(doc, GATEPASS_SENSITIVE_FIELDS)
    )
    return str(res.inserted_id)


async def test_billing_report_is_tenant_scoped(mocked_db):
    from bill_service.routers import bills as bills_router

    from bill_service.routers.bills import SENSITIVE_FIELDS as BILL_SENSITIVE

    for laundry_id in (LAUNDRY_A, LAUNDRY_B):
        await mocked_db["bills_collection"].insert_one(encrypt_dict({
            "laundry_id": laundry_id,
            "client_name": "Hotel X",
            "grand_total": 100.0,
            "paid_amount": 0.0,
            "outstanding_amount": 100.0,
            "payment_status": "PENDING",
            "created_at": _now(),
        }, BILL_SENSITIVE))
    rep_a = await dashboard.get_billing_report(ua())
    rep_b = await dashboard.get_billing_report(ub())
    assert rep_a["total_sales"] == 100.0
    assert rep_b["total_sales"] == 100.0
    # cross-check: raw total would be 200 if unscoped
    raw = await mocked_db["bills_collection"].count_documents({})
    assert raw == 2


async def test_day_money_is_tenant_scoped(mocked_db):
    today = _now().date().isoformat()
    for laundry_id in (LAUNDRY_A, LAUNDRY_B):
        await mocked_db["bills_collection"].insert_one(encrypt_dict({
            "laundry_id": laundry_id,
            "client_name": "Hotel X",
            "grand_total": 50.0,
            "paid_amount": 0.0,
            "payment_status": "PENDING",
            "created_at": _now(),
        }, BILL_SENSITIVE))
    res = await day_money.day_money(today, ua())
    assert res["bills_created"] == 1
    assert res["billed_amount"] == 50.0


async def test_reconciliation_lists_only_own_passes(mocked_db):
    from bill_service.crypto_helper import encrypt_dict as _enc
    from bill_service.routers.bills import GATEPASS_SENSITIVE_FIELDS as _GP_SENS

    async def seed_mismatch(number, laundry_id):
        doc = {
            "laundry_id": laundry_id,
            "gate_pass_number": number,
            "client_name": "Hotel X",
            "status": "RECEIVED",
            "receiving_date": _now().isoformat(),
            "items": [{
                "item_name": "Pillow", "specification": None, "category": "Bed",
                "client_qty": 10, "received_qty": 4, "difference": -6, "rewashed": False,
            }],
            "notes": "",
            "created_at": _now(),
            "updated_at": _now(),
        }
        await mocked_db["gatepasses_collection"].insert_one(_enc(doc, _GP_SENS))

    await seed_mismatch("GP-R-1", LAUNDRY_A)
    await seed_mismatch("GP-R-2", LAUNDRY_B)
    rows_a = await reconciliation.reconciliation_issues(None, ua())
    rows_b = await reconciliation.reconciliation_issues(None, ub())
    assert {r["gate_pass_number"] for r in rows_a["items"]} == {"GP-R-1"}
    assert {r["gate_pass_number"] for r in rows_b["items"]} == {"GP-R-2"}


async def test_notifications_only_own_pending(mocked_db):
    await _seed_gp(mocked_db, "GP-N-1", LAUNDRY_A)
    await _seed_gp(mocked_db, "GP-N-2", LAUNDRY_B)
    feed_a = await notifications.gatepass_pending(client_name=None, current_user=ua())
    feed_b = await notifications.gatepass_pending(client_name=None, current_user=ub())
    assert {e["gate_pass_number"] for e in feed_a} == {"GP-N-1"}
    assert {e["gate_pass_number"] for e in feed_b} == {"GP-N-2"}


async def test_dispatch_crud_is_tenant_scoped(mocked_db):
    created_a = await dispatch.create_dispatch_job(
        DispatchCreate(job_type="delivery", client_name="Hotel X"), ua()
    )
    created_b = await dispatch.create_dispatch_job(
        DispatchCreate(job_type="pickup", client_name="Hotel Y"), ub()
    )
    listed_a = await dispatch.list_dispatch_jobs(
        client_name=None, status=None, assigned_to=None, job_type=None,
        scheduled_from=None, scheduled_to=None, current_user=ua(),
    )
    listed_b = await dispatch.list_dispatch_jobs(
        client_name=None, status=None, assigned_to=None, job_type=None,
        scheduled_from=None, scheduled_to=None, current_user=ub(),
    )
    assert [j["id"] for j in listed_a] == [created_a["id"]]
    assert [j["id"] for j in listed_b] == [created_b["id"]]

    from fastapi import HTTPException
    import pytest

    with pytest.raises(HTTPException):
        await dispatch.get_dispatch_job(created_a["id"], ub())


async def test_linen_crud_is_tenant_scoped(mocked_db):
    from fastapi import HTTPException
    import pytest

    made_a = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel X"), ua()
    )
    made_b = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel Y"), ub()
    )
    listed_a = await linens.list_linens(
        search=None, category=None, status_filter=None, client_name=None,
        condition=None, sort_by="created_at", sort_order="desc",
        skip=0, limit=50, current_user=ua(),
    )
    listed_b = await linens.list_linens(
        search=None, category=None, status_filter=None, client_name=None,
        condition=None, sort_by="created_at", sort_order="desc",
        skip=0, limit=50, current_user=ub(),
    )
    assert {i.linen_id for i in listed_a.items} == {made_a["linen_id"]}
    assert {i.linen_id for i in listed_b.items} == {made_b["linen_id"]}
    with pytest.raises(HTTPException):
        await linens.get_linen(made_a["id"], ub())


async def test_linen_event_history_is_returned(mocked_db):
    """The profile's Movement History must load the scans for that linen."""
    made = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel X"), ua()
    )
    await linens.scan_linen(made["id"], LinenScanAction(action="collect"), ua())

    history = await linens.get_linen_events(made["id"], skip=0, limit=50, current_user=ua())

    assert history.total == 1
    assert history.items[0].linen_id == made["linen_id"]
    assert history.items[0].to_status == "COLLECTED"


async def test_loyalty_is_per_laundry(mocked_db):
    acc_a = await loyalty.adjust_loyalty(
        LoyaltyAdjust(client_name="Hotel X", delta_points=10), ua()
    )
    acc_b = await loyalty.adjust_loyalty(
        LoyaltyAdjust(client_name="Hotel X", delta_points=20), ub()
    )
    assert acc_a["points"] == 10
    assert acc_b["points"] == 20
    seen_a = await loyalty.get_loyalty("Hotel X", ua())
    assert seen_a["points"] == 10


async def test_day_close_is_per_laundry(mocked_db):
    from bill_service.routers.day_close import DayCloseRequest

    closed_a = await day_close.close_day(
        DayCloseRequest(date=_now().date().isoformat(), totals={}), ua()
    )
    assert closed_a["laundry_id"] == LAUNDRY_A
    seen_b = await day_close.get_day_close(_now().date().isoformat(), ub())
    assert seen_b is None
    seen_a = await day_close.get_day_close(_now().date().isoformat(), ua())
    assert seen_a is not None


async def test_events_timeline_is_tenant_scoped(mocked_db):
    from bill_service.services.transaction_events import record_event

    await record_event(
        entity_type="gatepass", entity_id="x1", event_type="TEST_A",
        laundry_id=LAUNDRY_A,
    )
    await record_event(
        entity_type="gatepass", entity_id="x2", event_type="TEST_B",
        laundry_id=LAUNDRY_B,
    )
    rows_a = await events.list_events(
        entity_type=None, entity_id=None, gate_pass_id=None, event_type=None,
        date=None, date_from=None, date_to=None, skip=0, limit=200, current_user=ua(),
    )
    rows_b = await events.list_events(
        entity_type=None, entity_id=None, gate_pass_id=None, event_type=None,
        date=None, date_from=None, date_to=None, skip=0, limit=200, current_user=ub(),
    )
    assert {e["event_type"] for e in rows_a} == {"TEST_A"}
    assert {e["event_type"] for e in rows_b} == {"TEST_B"}

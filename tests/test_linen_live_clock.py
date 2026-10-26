"""QR scans and linen field changes must advance a per-laundry live clock.

Other sessions poll this clock and refetch; a missed bump leaves every other
user looking at stale status after a scan.
"""
from bill_service.models import LinenCreate, LinenScanAction, LinenUpdate
from bill_service.routers import linens
from conftest import LAUNDRY_A, LAUNDRY_B, tenant_user


def ua():
    return tenant_user("clock-a", LAUNDRY_A, "ADMIN")


def ub():
    return tenant_user("clock-b", LAUNDRY_B, "ADMIN")


async def test_scan_advances_live_clock_for_that_laundry_only(mocked_db):
    made = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel X"), ua()
    )
    before_a = await linens.get_live_clock(ua())
    before_b = await linens.get_live_clock(ub())

    await linens.scan_linen(made["id"], LinenScanAction(action="collect"), ua())

    after_a = await linens.get_live_clock(ua())
    after_b = await linens.get_live_clock(ub())
    assert after_a["clock"] > before_a["clock"]
    assert after_b["clock"] == before_b["clock"]


async def test_parameter_patch_advances_live_clock(mocked_db):
    made = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel X"), ua()
    )
    before = await linens.get_live_clock(ua())
    await linens.update_linen(
        made["id"], LinenUpdate(location="Press table", notes="rewash"), ua()
    )
    after = await linens.get_live_clock(ua())
    assert after["clock"] > before["clock"]


async def test_bulk_scan_advances_live_clock_once(mocked_db):
    first = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel X"), ua()
    )
    second = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Duvet", client_name="Hotel X"), ua()
    )
    before = await linens.get_live_clock(ua())
    await linens.bulk_scan_linens(
        [first["linen_id"], second["linen_id"]],
        "receive",
        location="Intake",
        user=None,
        current_user=ua(),
    )
    after = await linens.get_live_clock(ua())
    assert after["clock"] == before["clock"] + 1

"""Response-shape contract for the linen API, frozen before the lifecycle redesign.

This is a compatibility guard, not a behaviour test. Every field name the web
app, the mobile scanner and the reports currently read is pinned here, so a
later phase that renames, drops or re-types one fails loudly instead of
silently breaking a client that deploys on a different cadence.

Three current defects are pinned as *behaviour*, each with a note saying which
phase must change it. Freezing a bug on purpose is the point: it makes the fix
a visible, deliberate edit rather than an undocumented side effect.

* A lookup by code is a write (`investigation D28`).
* `POST`/`GET`/`PATCH`/`scan` return raw documents, so they expose internal
  fields that the two response-modelled endpoints filter out (D30).
* Bulk scan aborts the whole request on the first unknown code instead of
  reporting it per line (D31).

Transition legality is deliberately NOT asserted: there is none today, so every
action succeeds from every status. The phase that introduces a `TRANSITIONS`
table must edit `test_every_action_lands_on_its_contracted_status` on purpose.
"""
from bill_service.models import LinenCreate, LinenEventModel, LinenScanAction, LinenUpdate
from bill_service.routers import linens
from fastapi import HTTPException
from conftest import LAUNDRY_A, LAUNDRY_B, tenant_user

# Today's action -> status contract (routers/linens.py:383-394).
ACTION_CONTRACT = {
    "collect": "COLLECTED",
    "receive": "AT_LAUNDRY",
    "start_wash": "WASHING",
    "complete_wash": "DRYING",
    "press": "PRESSING",
    "ready": "READY",
    "deliver": "DELIVERED",
    "mark_missing": "MISSING",
    "mark_damaged": "DAMAGED",
    "retire": "RETIRED",
}

# What `LinenModel` promises — the shape of `GET /linens` items.
LINEN_KEYS = {
    "id", "linen_id", "category", "item_type", "description", "size", "color",
    "client_name", "department", "status", "condition", "location",
    "wash_count", "last_washed_date", "last_scanned_date", "retirement_date",
    "notes", "created_at", "updated_at",
}

# What the un-modelled endpoints actually return: the stored document, with
# internal fields still attached. Pinning both sets is what catches a future
# accidental rename of either.
RAW_LINEN_KEYS = LINEN_KEYS | {"laundry_id", "client_name_search"}

EVENT_KEYS = {
    "id", "linen_id", "action", "from_status", "to_status", "location",
    "user", "related_order", "notes", "timestamp",
}


def ua():
    return tenant_user("contract-a", LAUNDRY_A, "ADMIN")


def ub():
    return tenant_user("contract-b", LAUNDRY_B, "ADMIN")


async def _make(client_name="Hotel Contract"):
    return await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name=client_name), ua()
    )


async def _list(user=None):
    return await linens.list_linens(
        search=None, category=None, status_filter=None, client_name=None,
        condition=None, sort_by="created_at", sort_order="desc",
        skip=0, limit=50, current_user=user or ua(),
    )


async def test_write_paths_return_the_raw_document(mocked_db):
    """D30, pinned: create/get/patch/scan expose fields the list endpoint hides.

    `POST`, `GET /{id}`, `GET /by-code`, `PATCH` and `POST /scan` declare no
    `response_model`, so `_serialize` hands back the stored document including
    `laundry_id` and the `client_name_search` normalisation helper. Only `laundry_id`
    matters: it is the caller's own tenant id, so this is not a cross-tenant leak,
    but it IS a published contract the mobile client now parses. The phase that
    adds shipment fields should put response models on these paths and update
    this test.
    """
    made = await _make()
    assert set(made) == RAW_LINEN_KEYS
    assert made["laundry_id"] == LAUNDRY_A
    assert made["linen_id"].startswith("LL-")
    assert isinstance(made["id"], str), "document id must serialise as a string"
    assert isinstance(made["wash_count"], int)

    assert set(await linens.get_linen(made["id"], ua())) == RAW_LINEN_KEYS
    assert set(await linens.get_linen_by_code(made["linen_id"], ua())) == RAW_LINEN_KEYS
    assert set(await linens.scan_linen(made["id"], LinenScanAction(action="collect"), ua())) == RAW_LINEN_KEYS


async def test_read_modelled_paths_filter_the_document(mocked_db):
    made = await _make()

    listing = await _list()
    assert set(listing.model_dump()) == {"items", "total"}
    assert listing.total == 1
    assert set(listing.items[0].model_dump()) == LINEN_KEYS

    single = await linens.get_linen(made["id"], ua())
    assert single["id"] == made["id"]

    stats = await linens.get_linen_stats(ua())
    assert "total" in stats.model_dump()
    assert stats.total == 1


async def test_by_code_is_case_insensitive(mocked_db):
    """Tags reach the API from scanners that may or may not upper-case them."""
    made = await _make()
    lowered = made["linen_id"].lower()
    looked = await linens.get_linen_by_code(lowered, ua())
    assert looked["linen_id"] == made["linen_id"]


async def test_lookup_is_a_write_and_journals_nothing(mocked_db):
    """D28, pinned: a lookup stamps state and bumps the live clock.

    `GET /linens/by-code/{code}` updates `last_scanned_date`/`updated_at` and
    calls `_note_change(..., "lookup")` while creating no event. Clients read
    `last_scanned_date` as "last seen", so the phase that makes this an honest
    read must move the stamping into the scan path and update this test.
    """
    made = await _make()
    clock_before = (await linens.get_live_clock(ua()))["clock"]

    looked = await linens.get_linen_by_code(made["linen_id"], ua())
    assert looked["last_scanned_date"] is not None
    assert (await linens.get_live_clock(ua()))["clock"] > clock_before

    events = await linens.get_linen_events(made["id"], skip=0, limit=50, current_user=ua())
    assert [e.action for e in events.items] == [], "a lookup must not journal a scan"


async def test_scan_writes_state_and_one_journal_row(mocked_db):
    made = await _make()
    scanned = await linens.scan_linen(
        made["id"], LinenScanAction(action="collect", location="Intake"), ua()
    )
    assert scanned["status"] == "COLLECTED"
    assert scanned["location"] == "Intake"

    events = await linens.get_linen_events(made["id"], skip=0, limit=50, current_user=ua())
    assert set(events.model_dump()) == {"items", "total"}
    event = events.items[0]
    assert set(event.model_dump()) == EVENT_KEYS, "event response fields changed"
    # LinenEventModel has no laundry_id, so the tenant field is filtered out of
    # the response even though the stored journal row carries it.
    assert event.from_status == made["status"]
    assert event.to_status == "COLLECTED"
    assert "laundry_id" not in event.model_dump()
    stored = await mocked_db["linen_events_collection"].find_one({"linen_id": made["linen_id"]})
    assert stored["laundry_id"] == LAUNDRY_A


async def test_every_action_lands_on_its_contracted_status(mocked_db):
    """No legality check today: each action succeeds from the initial status."""
    for action, expected in ACTION_CONTRACT.items():
        made = await _make(client_name=f"Hotel {action}")
        scanned = await linens.scan_linen(made["id"], LinenScanAction(action=action), ua())
        assert scanned["status"] == expected, f"{action} must still produce {expected}"


async def test_complete_wash_increments_and_retire_stamps(mocked_db):
    made = await _make()
    washed = await linens.scan_linen(made["id"], LinenScanAction(action="complete_wash"), ua())
    assert washed["wash_count"] == made["wash_count"] + 1
    assert washed["last_washed_date"] is not None

    retired = await linens.scan_linen(made["id"], LinenScanAction(action="retire"), ua())
    assert retired["retirement_date"] is not None


async def test_unknown_action_is_rejected_on_both_paths(mocked_db):
    made = await _make()
    for attempt in (
        linens.scan_linen(made["id"], LinenScanAction(action="teleport"), ua()),
        linens.bulk_scan_linens([made["linen_id"]], "teleport", None, None, ua()),
    ):
        try:
            await attempt
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("an unknown action must be rejected")


async def test_bulk_scan_response_shape(mocked_db):
    first = await _make(client_name="Hotel One")
    second = await _make(client_name="Hotel Two")

    result = await linens.bulk_scan_linens(
        [first["linen_id"], second["linen_id"]], "receive", "Intake", None, ua()
    )
    assert set(result) == {"processed", "errors", "total_processed", "total_errors"}
    assert result["total_processed"] == 2
    assert result["total_errors"] == 0
    assert set(result["processed"][0]) == {"linen_id", "from_status", "to_status"}


async def test_bulk_scan_aborts_on_the_first_unknown_code(mocked_db):
    """D31, pinned: the per-line `Not found` branch is unreachable.

    `bulk_scan_linens` looks the code up unscoped, then calls
    `assert_same_laundry(doc, …)`, which raises 404 for a missing document
    before `if not doc:` can append to `errors`. So one damaged or unregistered
    barcode kills the whole batch — after earlier lines are already committed —
    and the operator gets an opaque 404 instead of a per-line result. Phase 2
    must report it per line, keep the 404 only for a genuinely foreign code,
    and update this test.
    """
    made = await _make()
    try:
        await linens.bulk_scan_linens([made["linen_id"], "LL-NOPE12"], "receive", None, None, ua())
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("today a bulk scan raises; the fix turns this into a per-line error")

    # The line that got through first is already committed — no rollback.
    after = await linens.get_linen(made["id"], ua())
    assert after["status"] == "AT_LAUNDRY"


async def test_cross_tenant_reads_are_indistinguishable_from_absent(mocked_db):
    """Isolation contract: another laundry's linen answers 404, never 403.

    A 403 would confirm the identifier exists and leak inventory size.
    """
    made = await _make()

    for call in (
        linens.get_linen(made["id"], ub()),
        linens.get_linen_by_code(made["linen_id"], ub()),
        linens.scan_linen(made["id"], LinenScanAction(action="collect"), ub()),
        linens.update_linen(made["id"], LinenUpdate(location="elsewhere"), ub()),
        linens.bulk_scan_linens([made["linen_id"]], "receive", None, None, ub()),
    ):
        try:
            await call
        except HTTPException as exc:
            assert exc.status_code == 404, "cross-tenant access must answer 404"
        else:
            raise AssertionError("another tenant reached this linen")

    assert (await _list(ub())).items == []
    assert (await _list(ub())).total == 0


async def test_live_clock_response_shape(mocked_db):
    assert set(await linens.get_live_clock(ua())) == {"clock"}


async def test_events_are_newest_first(mocked_db):
    made = await _make()
    for action in ("collect", "receive", "start_wash"):
        await linens.scan_linen(made["id"], LinenScanAction(action=action), ua())

    events = await linens.get_linen_events(made["id"], skip=0, limit=50, current_user=ua())
    assert [e.action for e in events.items] == ["start_wash", "receive", "collect"]
    assert events.total == 3
    assert all(isinstance(e, LinenEventModel) for e in events.items)

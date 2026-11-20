"""The scan-action catalogue the server publishes, and its agreement with the writer.

`GET /linens/transitions` exists so no client keeps its own copy of the status
vocabulary. That only holds if the catalogue and `scan_linen` cannot disagree,
which is what most of these tests pin: a card the operator taps must produce
the status the server writes.
"""
from bill_service.models import LINEN_STATUSES, LinenCreate, LinenScanAction
from bill_service.routers import linens
from conftest import LAUNDRY_A, LAUNDRY_B, tenant_user


def ua():
    return tenant_user("transitions-a", LAUNDRY_A, "ADMIN")


def staff():
    return tenant_user("transitions-staff", LAUNDRY_A, "STAFF")


def other_tenant():
    return tenant_user("transitions-b", LAUNDRY_B, "ADMIN")


async def test_catalogue_covers_every_action_exactly_once():
    catalogue = linens._transition_catalogue()
    published = [t.action for g in catalogue for t in g.transitions]

    assert sorted(published) == sorted(linens.ACTION_STATUS_MAP)
    assert len(published) == len(set(published)), "an action appeared in two groups"


async def test_every_published_status_is_a_real_status():
    """A card must never name a status that `LINEN_STATUSES` does not contain."""
    for group in linens._transition_catalogue():
        for transition in group.transitions:
            assert transition.to_status in LINEN_STATUSES


async def test_published_status_label_matches_the_status_name():
    assert linens._status_label("AT_LAUNDRY") == "At Laundry"
    assert linens._status_label("MARK_DAMAGED") == "Mark Damaged"


async def test_group_ordering_is_hotel_then_laundry_then_exceptions():
    groups = linens._transition_catalogue()
    assert [g.custody for g in groups] == ["hotel", "laundry", "exception"]
    assert all(len(g.transitions) > 0 for g in groups), "an empty picker group"


async def test_custody_is_declared_for_every_action():
    assert set(linens.ACTION_CUSTODY) == set(linens.ACTION_STATUS_MAP), (
        "an action without a custody group would vanish from the picker"
    )


async def test_endpoint_shape(mocked_db):
    result = await linens.list_transitions(staff())
    assert set(result.model_dump()) == {"groups", "statuses"}
    assert result.statuses == LINEN_STATUSES

    first = result.groups[0].transitions[0]
    assert set(first.model_dump()) == {
        "action", "label", "to_status", "to_status_label", "custody", "terminal"
    }


async def test_catalogue_and_the_write_path_agree(mocked_db):
    """The contract that matters: tapping a card yields the status it promised.

    If `scan_linen` ever maps an action somewhere else, the picker lies to the
    operator about where the linen went.
    """
    catalogue = {
        t.action: t
        for group in linens._transition_catalogue()
        for t in group.transitions
    }
    for action, transition in catalogue.items():
        made = await linens.create_linen(
            LinenCreate(category="Bed", item_type="Sheet", client_name=f"Hotel {action}"), ua()
        )
        scanned = await linens.scan_linen(made["id"], LinenScanAction(action=action), ua())
        assert scanned["status"] == transition.to_status, (
            f"{action}: catalogue promises {transition.to_status}, writer produced {scanned['status']}"
        )


async def test_read_capability_is_enough(mocked_db):
    """A staff member choosing a target state needs no write grant."""
    assert (await linens.list_transitions(staff())).groups


async def test_catalogue_is_not_tenant_data(mocked_db):
    """The state machine is shared: another laundry sees the same shapes.

    Nothing tenant-owned is in the response, so this cannot leak — which is the
    point of keeping it a description of the machine rather than of the data.
    """
    mine = await linens.list_transitions(other_tenant())
    theirs = await linens.list_transitions(ua())
    assert [g.model_dump() for g in mine.groups] == [g.model_dump() for g in theirs.groups]

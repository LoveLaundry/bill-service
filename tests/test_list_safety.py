"""Regression tests for list-endpoint safety fixed alongside the Income/Ledger tabs.

Covers:
 * ``GET /gatepasses`` ``skip``/``limit`` pagination (the Income/Ledger tabs
   page through the full receiving history);
 * the ``$convert`` guard in the bills gate-pass-date ``$lookup`` stage, which
   replaced a ``$toObjectId`` that collapsed the whole listing into a 500 as
   soon as a manual bill (``gate_pass_id=None``) met a gate-pass date filter;
 * ``search`` + ``client_name`` on ``GET /bills`` intersecting instead of
   ``search`` silently overriding the client scope (both resolve to the same
   ``client_name_search`` token field).
"""
from datetime import datetime, timezone

from bill_service.crypto_helper import encrypt_dict, get_search_token
from bill_service.routers import bills
from bill_service.routers import gatepasses as gatepasses_router
from bill_service.routers.bills import GATEPASS_SENSITIVE_FIELDS


def _now():
    return datetime.now(timezone.utc)


def _auth_user(name="Alice"):
    return {"auth_id": f"u-{name}", "user_name": name, "role": "ADMIN"}


def _gp_doc(number: str, client: str, receiving_date: str) -> dict:
    return {
        "gate_pass_number": number,
        "client_name": client,
        "client_name_search": get_search_token(client),
        "status": "RECEIVED",
        "receiving_date": receiving_date,
        "items": [],
        "notes": "",
        "created_at": _now(),
        "updated_at": _now(),
    }


async def _store_gp(mocked_db, number: str, client: str, receiving_date: str) -> str:
    encrypted = encrypt_dict(_gp_doc(number, client, receiving_date), GATEPASS_SENSITIVE_FIELDS)
    res = await mocked_db["gatepasses_collection"].insert_one(encrypted)
    return str(res.inserted_id)


def _bill_doc(client: str, **extra) -> dict:
    doc = {
        "gate_pass_id": None,
        "client_name": client,
        "client_name_search": get_search_token(client),
        "items": [],
        "total_amount": 0,
        "total_quantity": 0,
        "grand_total": 0,
        "outstanding_amount": 0,
        "paid_amount": 0,
        "payment_status": "PENDING",
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc.update(extra)
    return doc


async def _store_bill(mocked_db, client: str, **extra) -> str:
    encrypted = encrypt_dict(_bill_doc(client, **extra), bills.SENSITIVE_FIELDS)
    res = await mocked_db["bills_collection"].insert_one(encrypted)
    return str(res.inserted_id)


async def test_gatepass_list_paginates_with_skip_and_limit(mocked_db):
    await _store_gp(mocked_db, "GP-P1", "Hilton Colombo", "2026-01-01T00:00:00+00:00")
    await _store_gp(mocked_db, "GP-P2", "Hilton Colombo", "2026-01-02T00:00:00+00:00")
    await _store_gp(mocked_db, "GP-P3", "Hilton Colombo", "2026-01-03T00:00:00+00:00")

    first = await gatepasses_router.list_gate_passes(
        None, None, None, None, 0, 2, _auth_user()
    )
    assert [g["gate_pass_number"] for g in first] == ["GP-P3", "GP-P2"]

    second = await gatepasses_router.list_gate_passes(
        None, None, None, None, 2, 2, _auth_user()
    )
    assert [g["gate_pass_number"] for g in second] == ["GP-P1"]

    everything = await gatepasses_router.list_gate_passes(
        None, None, None, None, 0, 5000, _auth_user()
    )
    assert len(everything) == 3


def test_gatepass_lookup_stage_tolerates_null_ids():
    """The join stage must use $convert (null-safe), never bare $toObjectId."""
    stage = bills._gatepass_lookup_stage()
    expr = stage["$lookup"]["pipeline"][0]["$match"]["$expr"]["$eq"]
    assert expr[0] == "$_id"
    convert = expr[1]["$convert"]
    assert convert["input"] == "$$gp_id_str"
    assert convert["to"] == "objectId"
    assert convert["onError"] is None
    assert convert["onNull"] is None


def test_combine_name_filters_intersects_without_dropping():
    assert bills._combine_name_filters(None, None) == {}
    assert bills._combine_name_filters("tok-a", None) == {"client_name_search": "tok-a"}
    assert bills._combine_name_filters(None, "tok-b") == {"client_name_search": "tok-b"}
    assert bills._combine_name_filters("tok-a", "tok-b") == {
        "$and": [{"client_name_search": "tok-a"}, {"client_name_search": "tok-b"}]
    }


async def test_bill_search_and_client_name_intersect(mocked_db):
    await _store_bill(mocked_db, "Hilton Colombo")
    await _store_bill(mocked_db, "Galle Face Hotel")

    same = await bills.list_bills(
        "Hilton Colombo", "Hilton Colombo", None, None, None, None, None, None, 0, 20, _auth_user()
    )
    assert same["total"] == 1
    assert same["items"][0]["client_name"] == "Hilton Colombo"

    # Different exact names intersect to nothing — the client scope is never
    # silently overridden by `search`.
    mixed = await bills.list_bills(
        "Hilton Colombo", "Galle Face Hotel", None, None, None, None, None, None, 0, 20, _auth_user()
    )
    assert mixed["total"] == 0
    assert mixed["items"] == []

"""List-endpoint safety: gatepass skip/limit pagination, null-safe $convert in
the bills gate-pass-date lookup (bare $toObjectId 500s on manual bills), and
search + client_name intersecting instead of search overriding client scope.
"""
from datetime import datetime, timezone

from bill_service.crypto_helper import encrypt_dict, get_search_token
from bill_service.routers import bills
from bill_service.routers import gatepasses as gatepasses_router
from bill_service.routers.bills import GATEPASS_SENSITIVE_FIELDS


def _now():
    return datetime.now(timezone.utc)


def _auth_user(name="Alice"):
    from conftest import tenant_user

    return tenant_user(f"u-{name}")


def _gp_doc(number: str, client: str, receiving_date: str) -> dict:
    return {
        "laundry_id": "acme",
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
        "laundry_id": "acme",
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


async def _store_payment(mocked_db, bill_id: str, client: str, amount: float, payment_date) -> str:
    doc = {
        "laundry_id": "acme",
        "bill_id": bill_id,
        "client_name": client,
        "client_name_search": get_search_token(client),
        "amount": amount,
        "payment_date": payment_date,
        "payment_method": "CASH",
        "reference": None,
        "notes": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    encrypted = encrypt_dict(doc, bills.PAYMENT_SENSITIVE_FIELDS)
    res = await mocked_db["payments_collection"].insert_one(encrypted)
    return str(res.inserted_id)


async def test_bulk_payments_filter_by_client_and_date(mocked_db):
    await _store_payment(mocked_db, "bill-1", "Hilton Colombo", 100.0, datetime(2026, 1, 5, tzinfo=timezone.utc))
    await _store_payment(mocked_db, "bill-2", "Hilton Colombo", 50.0, datetime(2026, 2, 5, tzinfo=timezone.utc))
    await _store_payment(mocked_db, "bill-3", "Galle Face Hotel", 75.0, datetime(2026, 1, 5, tzinfo=timezone.utc))

    scoped = await bills.list_all_payments("Hilton Colombo", None, None, 0, 500, _auth_user())
    assert sorted(p["amount"] for p in scoped) == [50.0, 100.0]
    assert {p["bill_id"] for p in scoped} == {"bill-1", "bill-2"}

    ranged = await bills.list_all_payments(
        None, datetime(2026, 1, 1), datetime(2026, 1, 31), 0, 500, _auth_user()
    )
    assert sorted(p["amount"] for p in ranged) == [75.0, 100.0]

    paged = await bills.list_all_payments(None, None, None, 1, 1, _auth_user())
    assert len(paged) == 1


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
    stage = bills._gatepass_lookup_stage("lovelaundry")
    conditions = stage["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"]
    id_equality = conditions[0]["$eq"]
    assert id_equality[0] == "$_id"
    convert = id_equality[1]["$convert"]
    assert convert["input"] == "$$gp_id_str"
    assert convert["to"] == "objectId"
    assert convert["onError"] is None
    assert convert["onNull"] is None


def test_gatepass_lookup_stage_never_joins_another_tenant():
    """A bill must not attach a gate pass belonging to a different laundry."""
    legacy = bills._gatepass_lookup_stage("lovelaundry")
    legacy_conditions = legacy["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"]
    legacy_tenant = legacy_conditions[1]
    assert {"$eq": ["$laundry_id", "lovelaundry"]} in legacy_tenant["$or"]
    assert {"$eq": [{"$type": "$laundry_id"}, "missing"]} in legacy_tenant["$or"]

    strict = bills._gatepass_lookup_stage("acme")
    strict_conditions = strict["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"]
    assert strict_conditions[1] == {"$eq": ["$laundry_id", "acme"]}


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

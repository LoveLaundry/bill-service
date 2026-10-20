"""Tests for the idempotency key guard and its tenant/resource boundaries."""
from bson import ObjectId

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from bill_service.services import idempotency as idem


def _req(headers=None, path="/bills"):
    hdrs = [(k.lower().encode(), f"{v}".encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": hdrs,
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )


def test_no_header_returns_none():
    assert idem._get_key(_req(), "u1") is None


def test_key_is_namespaced_per_user():
    req = _req({"X-Idempotency-Key": "abc-123"})
    assert idem._get_key(req, "user-9") == "user-9:abc-123"
    # same key sent by another user never collides
    assert idem._get_key(req, "user-7") == "user-7:abc-123"


def test_scoped_key_separates_tenant_entity_and_route():
    req = _req({"X-Idempotency-Key": "abc-123"}, path="/bills/123/payments")
    key_a = idem._scoped_key(req, "user-9", "laundry-a", "payment")
    assert key_a == "user-9:laundry-a:payment:/bills/123/payments:abc-123"
    assert idem._scoped_key(req, "user-9", "laundry-b", "payment") != key_a
    assert idem._scoped_key(req, "user-9", "laundry-a", "bill") != key_a
    assert idem._scoped_key(
        _req({"X-Idempotency-Key": "abc-123"}, path="/bills/456/payments"),
        "user-9",
        "laundry-a",
        "payment",
    ) != key_a


def test_whitespace_only_key_rejected():
    with pytest.raises(HTTPException) as exc:
        idem._get_key(_req({"X-Idempotency-Key": "   "}), "u")
    assert exc.value.status_code == 400


def test_overlong_key_rejected():
    req = _req({"X-Idempotency-Key": "x" * 129})
    with pytest.raises(HTTPException) as exc:
        idem._get_key(req, "u")
    assert exc.value.status_code == 400
    assert "X-Idempotency-Key" in exc.value.detail


async def test_find_previous_is_scoped_to_tenant_and_entity(mocked_db):
    request = _req({"X-Idempotency-Key": "retry-1"})
    document_id = ObjectId()
    docs = mocked_db["bills_collection"]
    await docs.insert_one({"_id": document_id, "laundry_id": "laundry-a"})
    await mocked_db["idempotency_collection"].insert_one(
        {
            "key": idem._scoped_key(request, "user-1", "laundry-a", "bill"),
            "entity_type": "bill",
            "entity_id": str(document_id),
        }
    )

    assert await idem.find_previous(
        request, "user-1", "laundry-a", "bill", docs
    ) is not None
    assert await idem.find_previous(
        request, "user-1", "laundry-b", "bill", docs
    ) is None
    assert await idem.find_previous(
        request, "user-1", "laundry-a", "payment", docs
    ) is None


async def test_legacy_key_replay_checks_document_tenant_and_type(mocked_db):
    request = _req({"X-Idempotency-Key": "legacy-retry"})
    docs = mocked_db["bills_collection"]
    document_id = ObjectId()
    await docs.insert_one({"_id": document_id, "laundry_id": "laundry-b"})
    await mocked_db["idempotency_collection"].insert_one(
        {
            "key": idem._get_key(request, "user-1"),
            "entity_type": "bill",
            "entity_id": str(document_id),
        }
    )

    assert await idem.find_previous(
        request, "user-1", "laundry-a", "bill", docs
    ) is None
    assert await idem.find_previous(
        request, "user-1", "laundry-b", "payment", docs
    ) is None
    assert await idem.find_previous(
        request, "user-1", "laundry-b", "bill", docs
    ) is not None
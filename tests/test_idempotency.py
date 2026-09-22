"""Unit tests for the idempotency key guard.

The pieces tested here are DB-free: header reading, namespacing and size
validation. The find/record paths are thin collection wrappers around these.
"""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from bill_service.services import idempotency as idem


def _req(headers=None):
    hdrs = [(k.lower().encode(), f"{v}".encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
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
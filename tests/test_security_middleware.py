"""Security middleware regression tests for bill_service.

Covers the hardened behaviour of the request layer:

  * the rate limiter buckets by the real client address, so one visitor behind
    the reverse proxy cannot lock everybody else out (and a directly exposed
    service cannot be tricked into spoofing another client's bucket),
  * a client-supplied ``X-Request-Id`` is echoed only when it is well formed,
  * HSTS is sent only over HTTPS.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")
os.environ.setdefault("MASTER_KEY", "test-only-master-key-0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bill_service import security

# The reverse proxy in front of the service sits on the private docker
# network; visitors reach it over the public internet.
PROXY_PEER = ("172.18.0.5", 50000)
PUBLIC_PEER = ("8.8.8.8", 50000)

VISITOR_A = "93.184.216.34"
VISITOR_B = "1.1.1.1"


def _app(limit: int = 300) -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    security.apply_security(app, rate_limit=limit)
    return app


@pytest.fixture
def client():
    return TestClient(_app())


def test_security_headers_are_present(client):
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "Permissions-Policy" in r.headers
    assert r.headers["X-Request-Id"]


def test_well_formed_request_id_is_echoed(client):
    r = client.get("/ping", headers={"X-Request-Id": "abc-123_45.6"})
    assert r.headers["X-Request-Id"] == "abc-123_45.6"


def test_malicious_request_id_is_replaced(client):
    """A header value must never reach the response verbatim."""
    for bad in ["a\r\nX-Injected: 1", "has space", "x" * 65, "<script>"]:
        r = client.get("/ping", headers={"X-Request-Id": bad})
        assert r.headers["X-Request-Id"] != bad
        assert "X-Injected" not in r.headers
        assert len(r.headers["X-Request-Id"]) == 16


def test_hsts_is_only_sent_over_https(client):
    plain = client.get("/ping")
    assert "Strict-Transport-Security" not in plain.headers

    secure = client.get("/ping", headers={"X-Forwarded-Proto": "https"})
    assert secure.headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_rate_limit_blocks_one_client_only():
    """The bucket must be per client address, not global."""
    limit_client = TestClient(_app(limit=2), client=PROXY_PEER)

    # Two different visitors behind the same proxy.
    r1 = limit_client.get("/ping", headers={"X-Real-IP": VISITOR_A})
    r2 = limit_client.get("/ping", headers={"X-Real-IP": VISITOR_B})
    assert r1.status_code == 200
    assert r2.status_code == 200

    # The first visitor exhausts their own bucket ...
    limit_client.get("/ping", headers={"X-Real-IP": VISITOR_A})
    blocked = limit_client.get("/ping", headers={"X-Real-IP": VISITOR_A})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers

    # ... without affecting the second one.
    assert limit_client.get("/ping", headers={"X-Real-IP": VISITOR_B}).status_code == 200


def test_forwarded_for_uses_the_last_entry():
    """nginx appends the address it saw, so the right-most entry is the real one."""
    limit_client = TestClient(_app(limit=1), client=PROXY_PEER)
    limit_client.get("/ping", headers={"X-Forwarded-For": VISITOR_A})
    blocked = limit_client.get("/ping", headers={"X-Forwarded-For": VISITOR_A})
    assert blocked.status_code == 429

    # A different client is unaffected.
    assert limit_client.get("/ping", headers={"X-Forwarded-For": VISITOR_B}).status_code == 200


def test_forwarded_headers_are_ignored_from_a_public_peer():
    """A directly exposed service must not trust client-supplied headers."""
    limit_client = TestClient(_app(limit=1), client=PUBLIC_PEER)
    assert limit_client.get("/ping", headers={"X-Real-IP": VISITOR_A}).status_code == 200
    # The spoofed header did not create a second bucket: the same peer is
    # still rate limited by its real address.
    assert limit_client.get("/ping", headers={"X-Real-IP": VISITOR_A}).status_code == 429


def test_client_key_uses_forwarded_address_from_a_local_proxy():
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/ping",
        "headers": [(b"x-real-ip", VISITOR_A.encode())],
        "client": PROXY_PEER,
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
        "root_path": "",
    }
    assert security._client_key(Request(scope)) == VISITOR_A


def test_client_key_ignores_forwarded_address_from_a_public_peer():
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/ping",
        "headers": [
            (b"x-real-ip", VISITOR_A.encode()),
            (b"x-forwarded-for", VISITOR_B.encode()),
        ],
        "client": PUBLIC_PEER,
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
        "root_path": "",
    }
    assert security._client_key(Request(scope)) == PUBLIC_PEER[0]

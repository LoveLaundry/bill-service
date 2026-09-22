"""Unit tests for the verification state machine.

The repository/collection calls are monkeypatched so the logic can be
exercised without a database.
"""
import asyncio

from bill_service.services import verification_service as vs


class _FakeCollection:
    def __init__(self):
        self.updates = []

    async def update_one(self, filter_, update, upsert=False):
        self.updates.append((filter_, update, upsert))
        return object()


def test_get_verification_defaults_when_no_row(monkeypatch):
    async def _no_row(entity, record_id):
        return None

    monkeypatch.setattr(vs, "get_sync_status", _no_row)
    payload = asyncio.run(vs.get_verification("bill", "x"))
    assert payload == {"status": "PENDING", "verified": False, "last_verified_at": None}


def test_get_verification_maps_row(monkeypatch):
    async def _row(entity, record_id):
        return {
            "status": "VERIFIED",
            "last_verified_at": "2026-09-22T00:00:00Z",
            "main_version": 3,
            "secondary_version": 3,
            "error": None,
        }

    monkeypatch.setattr(vs, "get_sync_status", _row)
    payload = asyncio.run(vs.get_verification("bill", "x"))
    assert payload["verified"] is True
    assert payload["status"] == vs.STATUS_VERIFIED
    assert payload["main_version"] == 3


def test_verify_marks_failed_when_compare_raises(monkeypatch):
    async def _boom(entity, record_id, version, now):
        raise RuntimeError("secondary down")

    recorded = {}

    async def _record(entity, record_id, main_version, status, error=None):
        recorded["status"] = status
        recorded["error"] = error

    monkeypatch.setattr(vs, "verify_document", _boom)
    monkeypatch.setattr(vs, "record_sync_status", _record)
    status = asyncio.run(vs.verify_against_secondary("bill", "x", 1))
    assert status == vs.STATUS_FAILED
    assert recorded["error"] == "secondary down"


def test_verify_returns_pending_on_version_mismatch(monkeypatch):
    async def _mismatch(entity, record_id, version, now):
        return False

    calls = []

    async def _record(entity, record_id, main_version, status, error=None):
        calls.append(status)

    monkeypatch.setattr(vs, "verify_document", _mismatch)
    monkeypatch.setattr(vs, "record_sync_status", _record)
    status = asyncio.run(vs.verify_against_secondary("bill", "x", 1))
    assert status == vs.STATUS_PENDING
    assert calls == [vs.STATUS_PENDING]


def test_verify_marks_verified_when_matched(monkeypatch):
    async def _match(entity, record_id, version, now):
        return True

    fake = _FakeCollection()
    monkeypatch.setattr(vs, "verify_document", _match)
    monkeypatch.setattr(vs, "sync_status_collection", fake)
    status = asyncio.run(vs.verify_against_secondary("bill", "x", 2))
    assert status == vs.STATUS_VERIFIED
    assert len(fake.updates) == 1
    assert fake.updates[0][1]["$set"]["status"] == vs.STATUS_VERIFIED
    assert fake.updates[0][1]["$set"]["secondary_version"] == 2
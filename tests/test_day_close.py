"""Unit tests for the day-close ordering guardrails."""
from datetime import date

import pytest
from fastapi import HTTPException

from bill_service.routers.day_close import _parse_day, _validate_date, ensure_not_future


def test_parse_valid_day():
    assert _parse_day("2026-09-22") == date(2026, 9, 22)


def test_parse_invalid_day_rejected():
    with pytest.raises(HTTPException) as exc:
        _parse_day("not-a-date")
    assert exc.value.status_code == 422


def test_validation_builds_end_of_day_utc():
    dt = _validate_date("2026-09-22")
    assert dt.hour == 23 and dt.minute == 59 and dt.second == 59
    assert dt.tzinfo is not None


def test_future_day_rejected():
    with pytest.raises(HTTPException) as exc:
        ensure_not_future(date(2026, 9, 30), today=date(2026, 9, 22))
    assert exc.value.status_code == 409
    assert "future day" in exc.value.detail


def test_today_and_next_day_allowed():
    # today itself and the next calendar day (UTC→local headroom) pass
    ensure_not_future(date(2026, 9, 22), today=date(2026, 9, 22))
    ensure_not_future(date(2026, 9, 23), today=date(2026, 9, 22))


def test_past_days_always_allowed():
    ensure_not_future(date(2026, 9, 1), today=date(2026, 9, 22))
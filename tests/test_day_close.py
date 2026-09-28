"""Unit tests for the day-close ordering guardrails."""
from datetime import date, timedelta, timezone

import pytest
from fastapi import HTTPException

from bill_service.app_time import LKT
from bill_service.routers.day_close import _parse_day, _validate_date, ensure_not_future


def test_parse_valid_day():
    assert _parse_day("2026-09-22") == date(2026, 9, 22)


def test_parse_invalid_day_rejected():
    with pytest.raises(HTTPException) as exc:
        _parse_day("not-a-date")
    assert exc.value.status_code == 422


def test_validation_builds_end_of_day_lkt():
    dt = _validate_date("2026-09-22")
    assert dt.hour == 23 and dt.minute == 59 and dt.second == 59
    assert dt.tzinfo is not None
    # The business day closes at 23:59:59 Sri Lankan time, which is 18:29:59 UTC.
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)
    assert dt.astimezone(timezone.utc).hour == 18


def test_day_end_is_inside_its_own_business_day():
    """A day-close event must sort within the day it closes, not the next one."""
    dt = _validate_date("2026-09-22")
    assert dt.astimezone(LKT).date() == date(2026, 9, 22)


def test_future_day_rejected():
    with pytest.raises(HTTPException) as exc:
        ensure_not_future(date(2026, 9, 30), reference_day=date(2026, 9, 22))
    assert exc.value.status_code == 409
    assert "future day" in exc.value.detail


def test_today_allowed():
    # "Today" is the current Sri Lankan date, so closing it is allowed.
    ensure_not_future(date(2026, 9, 22), reference_day=date(2026, 9, 22))


def test_tomorrow_rejected():
    # No more UTC→LKT headroom is needed now that both sides speak LKT, so
    # tomorrow is genuinely in the future and must be refused.
    with pytest.raises(HTTPException) as exc:
        ensure_not_future(date(2026, 9, 23), reference_day=date(2026, 9, 22))
    assert exc.value.status_code == 409


def test_past_days_always_allowed():
    ensure_not_future(date(2026, 9, 1), reference_day=date(2026, 9, 22))

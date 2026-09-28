"""Regression tests for the Sri Lankan business-day boundary.

These pin the behaviour that matters operationally: a shift that runs from
20:00 to 04:00 crosses midnight UTC but not midnight in Colombo, and every
report has to agree about which day a timestamp belongs to.
"""
from datetime import date, datetime, timedelta, timezone

from bill_service.app_time import (
    LKT,
    UTC,
    add_months,
    day_bounds,
    day_end,
    day_query,
    day_query_end,
    day_start,
    lkt,
    lkt_date_str,
    lkt_month_key,
    month_bounds,
    month_start,
    month_start_shift,
    naive_stamp,
    parse_day,
    parse_wall_clock,
    stamp,
    week_start,
    wall_clock,
    year_bounds,
)

# 2026-09-28 is a Monday. These instants sit either side of both midnights.
SEP_27_2300_UTC = datetime(2026, 9, 27, 23, 0, tzinfo=UTC)   # 2026-09-28 04:30 LKT
SEP_28_1900_UTC = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)   # 2026-09-29 00:30 LKT
SEP_28_1800_UTC = datetime(2026, 9, 28, 18, 0, tzinfo=UTC)   # 2026-09-28 23:30 LKT


def test_offset_is_plus_five_thirty():
    assert LKT.utcoffset(None) == timedelta(hours=5, minutes=30)


def test_late_evening_utc_is_already_tomorrow_in_colombo():
    # 19:00 UTC is 00:30 the next day in Sri Lanka.
    assert lkt_date_str(SEP_28_1900_UTC) == "2026-09-29"
    assert SEP_28_1900_UTC.date().isoformat() == "2026-09-28"


def test_evening_before_midnight_lkt_stays_on_the_same_day():
    assert lkt_date_str(SEP_28_1800_UTC) == "2026-09-28"


def test_early_morning_utc_is_later_the_same_day_in_colombo():
    assert lkt_date_str(SEP_27_2300_UTC) == "2026-09-28"


def test_naive_input_is_read_as_utc_not_local():
    """Mongo hands back naive UTC; it must not be reinterpreted as wall clock."""
    naive = datetime(2026, 9, 28, 19, 0)
    assert lkt(naive) == SEP_28_1900_UTC
    assert lkt_date_str(naive) == "2026-09-29"


def test_month_bucket_follows_colombo_not_utc():
    assert lkt_month_key(SEP_28_1800_UTC) == "2026-09"
    # 00:30 on the 29th LKT is still the September bucket; in UTC it would be
    # the 28th, which is the same month here — the interesting case is the 1st.
    first = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)   # 05:30 LKT, same month
    assert lkt_month_key(first) == "2026-10"
    # 2026-10-01 00:00 LKT is 2026-09-30 18:30 UTC — the UTC month is September.
    boundary = datetime(2026, 9, 30, 18, 30, tzinfo=UTC)
    assert boundary.strftime("%Y-%m") == "2026-09"
    assert lkt_month_key(boundary) == "2026-10"


def test_day_bounds_cover_the_colombo_day():
    start, end = day_bounds(date(2026, 9, 28))
    assert start == datetime(2026, 9, 27, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 9, 28, 18, 30, tzinfo=UTC)
    # Nothing outside that window belongs to the day.
    assert start < SEP_28_1800_UTC < end
    assert not (start <= SEP_28_1900_UTC < end)


def test_day_end_is_inside_its_own_day():
    end = day_end(date(2026, 9, 28))
    start, exclusive = day_bounds(date(2026, 9, 28))
    assert start <= end < exclusive
    assert lkt_date_str(end) == "2026-09-28"


def test_day_start_is_colombo_midnight_not_utc_midnight():
    assert day_start(date(2026, 9, 28)) == datetime(2026, 9, 27, 18, 30, tzinfo=UTC)
    assert lkt(day_start(date(2026, 9, 28))).hour == 0


def test_parse_wall_clock_reads_as_our_midnight():
    parsed = parse_wall_clock("2026-09-28")
    assert parsed == day_start(date(2026, 9, 28))
    assert parsed.hour == 0
    # 18:30 UTC the day before, not 00:00 UTC.
    assert parsed == datetime(2026, 9, 27, 18, 30, tzinfo=UTC)


def test_wall_clock_passes_through_aware_values():
    """An explicit +05:30 from the client must keep its instant."""
    submitted = datetime.fromisoformat("2026-09-28T00:00:00+05:30")
    assert wall_clock(submitted) == submitted
    assert wall_clock(submitted) == datetime(2026, 9, 27, 18, 30, tzinfo=UTC)


def test_wall_clock_assumes_colombo_for_naive_input():
    naive = datetime(2026, 9, 28, 0, 0)
    assert wall_clock(naive) == datetime(2026, 9, 27, 18, 30, tzinfo=UTC)


def test_stamp_emits_an_explicit_offset():
    """A naive ISO string would be read as *the reader's* local time."""
    out = stamp(SEP_28_1900_UTC)
    assert out.endswith("+05:30")
    assert out.startswith("2026-09-29T00:30")


def test_naive_stamp_is_colombo_wall_clock():
    assert naive_stamp(SEP_28_1900_UTC) == "2026-09-29 00:30:00"


def test_week_start_is_monday_midnight_colombo():
    # 2026-09-28 is a Monday; 2026-09-30 is the Wednesday of the same week.
    assert week_start(date(2026, 9, 28)) == week_start(date(2026, 9, 30))
    assert lkt(week_start(date(2026, 9, 30))).weekday() == 0
    assert lkt(week_start(date(2026, 9, 30))).hour == 0
    # A Sunday belongs to the week that started the previous Monday.
    sunday = week_start(date(2026, 10, 4))
    assert lkt(sunday).date() == date(2026, 9, 28)


def test_month_bounds_span_the_whole_colombo_month():
    start, end = month_bounds(2026, 9)
    assert lkt(start) == datetime(2026, 9, 1, 0, 0, tzinfo=LKT)
    assert lkt(end) == datetime(2026, 10, 1, 0, 0, tzinfo=LKT)
    # The last moment of 30 September in UTC is still inside September.
    assert start <= SEP_28_1900_UTC < end
    assert SEP_28_1900_UTC < end


def test_december_rolls_the_year():
    start, end = month_bounds(2026, 12)
    assert lkt(end) == datetime(2027, 1, 1, 0, 0, tzinfo=LKT)


def test_year_bounds():
    start, end = year_bounds(2026)
    assert lkt(start) == datetime(2026, 1, 1, 0, 0, tzinfo=LKT)
    assert lkt(end) == datetime(2027, 1, 1, 0, 0, tzinfo=LKT)


def test_add_months_clamps_the_day():
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2026, 1, 15), -1) == date(2025, 12, 15)


def test_parse_day_roundtrip():
    assert parse_day("2026-09-28") == date(2026, 9, 28)
    for bad in ("", "28-09-2026", "2026-13-01", "2026-09-31"):
        try:
            parse_day(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse")


def test_day_query_reads_an_aware_value_as_an_instant():
    """A client in another zone asking for a date means the Sri Lankan date."""
    # 2026-09-28T00:00Z is 05:30 LKT on the 28th.
    assert day_query(datetime(2026, 9, 28, 0, 0, tzinfo=UTC)) == day_start(date(2026, 9, 28))
    # 2026-09-28T20:00Z is 01:30 LKT on the 29th.
    assert day_query(datetime(2026, 9, 28, 20, 0, tzinfo=UTC)) == day_start(date(2026, 9, 29))


def test_day_query_treats_naive_input_as_our_wall_clock():
    assert day_query(datetime(2026, 9, 28, 0, 0)) == day_start(date(2026, 9, 28))
    assert day_query_end(datetime(2026, 9, 28, 0, 0)) == day_end(date(2026, 9, 28))


def test_day_query_end_is_the_last_moment_of_the_day():
    start, exclusive = day_bounds(date(2026, 9, 28))
    assert start <= day_query_end(datetime(2026, 9, 28, 12, 0)) < exclusive


def test_twelve_month_buckets_are_distinct():
    """The dashboard's yearly trend needs 12 distinct month keys."""
    keys = [lkt_month_key(month_start_shift(-i)) for i in range(12)]
    assert len(set(keys)) == 12
    assert keys[0] > keys[-1]  # oldest first

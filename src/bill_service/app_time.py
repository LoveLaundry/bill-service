"""Sri Lankan time — the only calendar this business runs on.

Love Laundry operates only in Sri Lanka, so every *business day* boundary,
"today", and month bucket is Asia/Colombo. That is a fixed UTC+05:30 offset
with no daylight saving, which is why this is a ``timedelta`` rather than a
``ZoneInfo``: it needs no tzdata package, and it behaves identically in a slim
container, on Vercel (which defaults to UTC) and on a developer's laptop.

Storage stays UTC — ``datetime.now(UTC)`` is still correct for stamping rows.
Only the *derivation of a calendar day from an instant* is timezone-sensitive,
and that is what lives here.

Do not use this module for JWT ``exp`` claims or HMAC replay windows: those are
wire-level instants that must stay UTC.
"""
from datetime import date, datetime, time, timedelta, timezone

#: Asia/Colombo — a fixed +05:30 offset, no DST.
LKT = timezone(timedelta(hours=5, minutes=30), "LKT")

#: Alias so call sites read clearly next to ``LKT``.
UTC = timezone.utc


def today() -> date:
    """Today's date on the Sri Lankan clock."""
    return datetime.now(UTC).astimezone(LKT).date()


def today_str() -> str:
    """Today as ``YYYY-MM-DD`` in Sri Lanka."""
    return today().isoformat()


def this_month() -> str:
    """The current Sri Lankan month as ``YYYY-MM``."""
    return today().strftime("%Y-%m")


def this_year() -> int:
    return today().year


def this_month_number() -> int:
    """The current Sri Lankan month as 1-12."""
    return today().month


def lkt(value: datetime) -> datetime:
    """The same instant, expressed on the Sri Lankan clock.

    Naive input is read as UTC, because that is how the database hands it back
    when the Mongo codec is not configured — reinterpreting it as local time
    would silently shift every historical row by 5½ hours.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).astimezone(LKT)
    return value.astimezone(LKT)


def day_start(day: date) -> datetime:
    """The first instant of a Sri Lankan business day."""
    return datetime.combine(day, time.min, tzinfo=LKT)


def day_end(day: date) -> datetime:
    """The closing instant of a Sri Lankan business day."""
    return datetime.combine(day, time(23, 59, 59), tzinfo=LKT)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """``(start, end_exclusive)`` covering one Sri Lankan business day.

    Used as a range filter: ``{"occurred_at": {"$gte": start, "$lt": end}}``.
    """
    start = day_start(day)
    return start, start + timedelta(days=1)


def parse_day(value: str) -> date:
    """Parse a ``YYYY-MM-DD`` business day."""
    return datetime.strptime(value, "%Y-%m-%d").date()


def week_start(day: date) -> datetime:
    """Monday of the week containing ``day``, at Sri Lankan midnight."""
    return day_start(day - timedelta(days=day.weekday()))


def month_start(year: int, month: int) -> datetime:
    """First instant of a Sri Lankan calendar month."""
    return day_start(date(year, month, 1))


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """``(start, end_exclusive)`` covering one Sri Lankan calendar month."""
    start = month_start(year, month)
    if month == 12:
        return start, month_start(year + 1, 1)
    return start, month_start(year, month + 1)


def year_bounds(year: int) -> tuple[datetime, datetime]:
    """``(start, end_exclusive)`` covering one Sri Lankan calendar year."""
    return day_start(date(year, 1, 1)), day_start(date(year + 1, 1, 1))
def add_months(day: date, months: int) -> date:
    """Shift a date by whole calendar months, clamping the day to the target.

    ``2026-03-31`` shifted by ``-1`` is ``2026-02-28``, not an invalid date.
    """
    total = (day.year * 12 + day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    if month == 12:
        last = (date(year + 1, 1, 1) - timedelta(days=1)).day
    else:
        last = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def month_start_shift(months: int) -> datetime:
    """Midnight on the first day of the month ``months`` away from this one."""
    shifted = add_months(today().replace(day=1), months)
    return month_start(shifted.year, shifted.month)


#: IANA name to hand to Mongo's ``$dateToString`` / ``$dateTrunc``. The ``$month``
#: and ``$dayOfWeek`` aggregation operators have no timezone option at all, so
#: any pipeline that buckets dates has to go through one of these instead.
MONGO_TIME_ZONE = "Asia/Colombo"


def parse_wall_clock(value: str) -> datetime:
    """A user-entered ``YYYY-MM-DD`` read as *our* midnight, not UTC midnight.

    A date typed into a form is a wall-clock intention, so it becomes an
    aware datetime at Asia/Colombo midnight. Storing that aware value means the
    instant is right regardless of the server's own timezone.
    """
    return day_start(parse_day(value))


def wall_clock(value: datetime) -> datetime:
    """Interpret a submitted timestamp, defaulting naive input to Sri Lanka.

    The API is called with an explicit ``+05:30`` offset, so an aware value
    already carries the right instant and is passed through untouched. Naive
    input is a legacy client that sent no offset; it is read as *our* wall
    clock. Either way the result is aware, which is what gets stored.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=LKT)
    return value


def day_query(value: datetime) -> datetime:
    """Start-of-Sri Lankan-day bound for a submitted date filter.

    An aware value is read as an instant first, so a client in another zone
    asking for "2026-09-28" still means the Sri Lankan 28th.
    """
    return day_start(lkt(value).date())


def day_query_end(value: datetime) -> datetime:
    """End-of-Sri Lankan-day bound for a submitted date filter (inclusive)."""
    return day_end(lkt(value).date())


def lkt_date_str(value: datetime) -> str:
    """The Sri Lankan calendar date (``YYYY-MM-DD``) an instant falls on."""
    return lkt(value).strftime("%Y-%m-%d")


def lkt_month_key(value: datetime) -> str:
    """The ``YYYY-MM`` bucket an instant falls into, in Sri Lanka."""
    return lkt(value).strftime("%Y-%m")


def lkt_month_label(value: datetime) -> str:
    """A ``Sep 26`` style bucket label, in Sri Lanka."""
    return lkt(value).strftime("%b %y")


def lkt_year(value: datetime) -> int:
    return lkt(value).year


def lkt_month_number(value: datetime) -> int:
    return lkt(value).month


def lkt_day_of_week(value: datetime) -> int:
    """Weekday 1-7 (Monday=1), matching Mongo's ``$dayOfWeek``, in Sri Lanka."""
    return lkt(value).isoweekday()


def naive_stamp(value: datetime) -> str:
    """``YYYY-MM-DD HH:MM:SS`` on the Sri Lankan clock, no offset suffix.

    For the legacy fields that were historically written as naive UTC strings.
    """
    return lkt(value).strftime("%Y-%m-%d %H:%M:%S")


def utc_iso_z() -> str:
    """UTC ISO-8601 with a ``Z`` suffix, for the change-history string fields.

    Byte-identical to the old ``datetime.utcnow().isoformat() + "Z"`` but from
    an aware datetime, so it is no longer deprecated and cannot drift with the
    host timezone.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def stamp(value: datetime) -> str:
    """ISO-8601 for API output, carrying the explicit Sri Lankan offset.

    Emitting an offset matters: a naive ``2026-09-28T10:00:00`` is interpreted by
    browsers (and most JSON consumers) as *their* local time, which is exactly
    the ambiguity this module exists to remove.
    """
    return lkt(value).isoformat()

"""All timestamps are stored as naive UTC so SQLite and Postgres behave the same."""

import calendar
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(dt_utc: datetime, tz: str) -> datetime:
    return dt_utc.replace(tzinfo=UTC).astimezone(ZoneInfo(tz))


def local_to_utc(dt_local: datetime) -> datetime:
    return dt_local.astimezone(UTC).replace(tzinfo=None)


def add_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def in_window(now_utc: datetime, start: time, end: time, tz: str) -> bool:
    t = to_local(now_utc, tz).time()
    return start <= t < end


def next_window_start(now_utc: datetime, start: time, end: time, tz: str) -> datetime:
    """Return now if inside the window, otherwise the next time the window opens."""
    if in_window(now_utc, start, end, tz):
        return now_utc
    local = to_local(now_utc, tz)
    candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return local_to_utc(candidate)

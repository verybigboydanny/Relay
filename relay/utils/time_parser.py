from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from relay.utils.errors import PlanError

UTC = timezone.utc
ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}
_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class TimeError(PlanError):
    code = "bad_time"


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(ISO_FMT)


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO_FMT).replace(tzinfo=UTC)


def tz_of(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise TimeError(f"unknown timezone '{name}'")


def parse_duration(text: str) -> timedelta:
    s = text.strip().lower().replace(" and ", " ")
    parts = _DUR.findall(s)
    if not parts or _DUR.sub("", s).replace(",", "").strip():
        raise TimeError(f"couldn't read '{text}' as a duration")
    total = 0.0
    for n, unit in parts:
        if unit not in _UNITS:
            raise TimeError(f"unknown time unit '{unit}'")
        total += float(n) * _UNITS[unit]
    if total <= 0:
        raise TimeError("duration must be positive")
    return timedelta(seconds=total)


def parse_local(text: str, tz: ZoneInfo) -> datetime:
    s = text.strip().replace("T", " ")
    if re.search(r"(Z|[+-]\d{2}:?\d{2})$", s):
        try:
            return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            raise TimeError(f"couldn't read '{text}' as a date and time")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=tz).astimezone(UTC)
    raise TimeError(f"couldn't read '{text}' as a date and time; I need a full date and time")


def parse_clock(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise TimeError(f"'{text}' isn't a valid HH:MM time")
    return int(m[1]), int(m[2])


def parse_weekdays(value) -> list[int]:
    items = value if isinstance(value, list) else [value]
    out = []
    for w in items:
        key = str(w).strip().lower()[:3]
        if key not in _WEEKDAYS:
            raise TimeError(f"unknown weekday '{w}'")
        out.append(_WEEKDAYS[key])
    return sorted(set(out))


def next_occurrence(rule: dict, after: datetime, tz: ZoneInfo) -> datetime:
    freq = rule["freq"]
    if freq == "interval":
        return after + parse_duration(rule["interval"])
    hh, mm = parse_clock(rule.get("time") or "09:00")
    local = after.astimezone(tz)

    def at(d):
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(UTC)

    if freq == "daily":
        for i in range(0, 3):
            c = at(local.date() + timedelta(days=i))
            if c > after:
                return c
    if freq == "weekly":
        days = parse_weekdays(rule.get("weekday") or "mon")
        for i in range(0, 9):
            d = local.date() + timedelta(days=i)
            if d.weekday() in days:
                c = at(d)
                if c > after:
                    return c
    if freq == "monthly":
        day = int(rule.get("day") or 1)
        if not 1 <= day <= 31:
            raise TimeError("day of month must be 1-31")
        y, m = local.year, local.month
        for _ in range(14):
            last = calendar.monthrange(y, m)[1]
            c = at(datetime(y, m, min(day, last)))
            if c > after:
                return c
            m += 1
            if m > 12:
                y, m = y + 1, 1
    raise TimeError(f"unsupported recurrence '{freq}'")


def describe_rule(rule: dict, tzname: str) -> str:
    f = rule["freq"]
    if f == "interval":
        return f"every {rule['interval']}"
    t = rule.get("time") or "09:00"
    if f == "daily":
        return f"every day at {t} {tzname}"
    if f == "weekly":
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"every {'/'.join(names[i] for i in parse_weekdays(rule.get('weekday') or 'mon'))} at {t} {tzname}"
    return f"monthly on day {rule.get('day') or 1} at {t} {tzname}"


def parse_period(text: str, tz: ZoneInfo, ref: datetime | None = None) -> tuple[datetime, datetime]:
    ref = (ref or now()).astimezone(tz)
    s = text.strip().lower().replace("_", " ")
    sod = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    if s == "today":
        return sod.astimezone(UTC), ref.astimezone(UTC)
    if s == "yesterday":
        return (sod - timedelta(days=1)).astimezone(UTC), sod.astimezone(UTC)
    if s == "this week":
        return (sod - timedelta(days=sod.weekday())).astimezone(UTC), ref.astimezone(UTC)
    if s == "last week":
        start = sod - timedelta(days=sod.weekday() + 7)
        return start.astimezone(UTC), (start + timedelta(days=7)).astimezone(UTC)
    m = re.fullmatch(r"(?:last|past)\s+(.+)", s)
    if m:
        try:
            return (ref - parse_duration(m[1])).astimezone(UTC), ref.astimezone(UTC)
        except TimeError:
            pass
    raise TimeError(f"couldn't read '{text}' as a time period")


def fmt_local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 90:
        return f"{s} seconds"
    if s < 5400:
        return f"{round(s / 60)} minutes"
    if s < 172800:
        return f"{round(s / 3600, 1):g} hours"
    return f"{round(s / 86400, 1):g} days"

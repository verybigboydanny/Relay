from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from relay.utils import time_parser as tp

UTC = timezone.utc
IST = tp.tz_of("Asia/Kolkata")
NY = tp.tz_of("America/New_York")


@pytest.mark.parametrize("s,secs", [("2 minutes", 120), ("10m", 600), ("1h30m", 5400), ("1 hour and 30 minutes", 5400), ("2 days", 172800), ("45 sec", 45)])
def test_durations(s, secs):
    assert tp.parse_duration(s).total_seconds() == secs


@pytest.mark.parametrize("s", ["soon", "", "5 parsecs", "-5 minutes", "0 minutes", "ten minutes"])
def test_bad_durations(s):
    with pytest.raises(tp.TimeError):
        tp.parse_duration(s)


def test_parse_local_uses_zone_and_rejects_partial():
    assert tp.parse_local("2026-11-01 17:00", IST) == datetime(2026, 11, 1, 11, 30, tzinfo=UTC)
    assert tp.parse_local("2026-11-01T17:00:00+00:00", IST) == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    for bad in ("tomorrow at 5", "17:00", "2026-11-01", "5 PM"):
        with pytest.raises(tp.TimeError):
            tp.parse_local(bad, IST)


def test_weekly_next_occurrence():
    after = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    nxt = tp.next_occurrence({"freq": "weekly", "weekday": "fri", "time": "19:00"}, after, IST)
    assert nxt == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    nxt2 = tp.next_occurrence({"freq": "weekly", "weekday": "fri", "time": "19:00"}, nxt, IST)
    assert nxt2 == nxt + timedelta(days=7)


def test_daily_and_interval_and_monthly():
    after = datetime(2026, 1, 30, 23, 0, tzinfo=UTC)
    assert tp.next_occurrence({"freq": "daily", "time": "09:00"}, after, UTC) == datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    assert tp.next_occurrence({"freq": "interval", "interval": "30 minutes"}, after, UTC) == after + timedelta(minutes=30)
    assert tp.next_occurrence({"freq": "monthly", "day": 31, "time": "08:00"}, datetime(2026, 2, 1, tzinfo=UTC), UTC) == datetime(2026, 2, 28, 8, 0, tzinfo=UTC)


def test_dst_wall_clock_is_kept():
    winter = tp.next_occurrence({"freq": "daily", "time": "09:00"}, datetime(2026, 3, 6, 20, 0, tzinfo=UTC), NY)
    summer = tp.next_occurrence({"freq": "daily", "time": "09:00"}, datetime(2026, 3, 9, 20, 0, tzinfo=UTC), NY)
    assert winter.astimezone(NY).hour == 9 and summer.astimezone(NY).hour == 9
    assert winter.hour != summer.hour


def test_periods():
    ref = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    s, u = tp.parse_period("yesterday", UTC, ref)
    assert (s, u) == (datetime(2026, 10, 29, tzinfo=UTC), datetime(2026, 10, 30, tzinfo=UTC))
    s, u = tp.parse_period("last 7 days", UTC, ref)
    assert u - s == timedelta(days=7)
    with pytest.raises(tp.TimeError):
        tp.parse_period("whenever", UTC, ref)


def test_iso_roundtrip():
    d = datetime(2026, 5, 1, 3, 4, 5, 6, tzinfo=UTC)
    assert tp.from_iso(tp.iso(d)) == d

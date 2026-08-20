from __future__ import annotations

from datetime import datetime, timedelta, timezone

from branch_maintenance.time_window import default_daily_2200_window

CST = timezone(timedelta(hours=8))


def test_default_window_before_today_2200():
    now = datetime(2026, 7, 31, 12, 43, 0, tzinfo=CST)
    since, until = default_daily_2200_window(now)
    assert since == "2026-07-29T22:00:00+08:00"
    assert until == "2026-07-30T22:00:00+08:00"


def test_default_window_after_today_2200():
    now = datetime(2026, 7, 31, 22, 0, 1, tzinfo=CST)
    since, until = default_daily_2200_window(now)
    assert since == "2026-07-30T22:00:00+08:00"
    assert until == "2026-07-31T22:00:00+08:00"


def test_default_window_exactly_2200_uses_that_boundary():
    now = datetime(2026, 7, 31, 22, 0, 0, tzinfo=CST)
    since, until = default_daily_2200_window(now)
    assert since == "2026-07-30T22:00:00+08:00"
    assert until == "2026-07-31T22:00:00+08:00"

"""Default evaluation time window helpers for BMA."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
EMPTY_SHA_MARK = "--"


def default_daily_2200_window(
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return ``(since, until)`` for the last completed 22:00~22:00 window (+08:00).

    Example: at 2026-07-31 12:00+08 → since=2026-07-29 22:00, until=2026-07-30 22:00.
    After 22:00 on 2026-07-31 → since=2026-07-30 22:00, until=2026-07-31 22:00.
    """
    if now is None:
        now = datetime.now(CST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    else:
        now = now.astimezone(CST)

    today_2200 = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if now < today_2200:
        until = today_2200 - timedelta(days=1)
    else:
        until = today_2200
    since = until - timedelta(days=1)
    return (
        since.isoformat(timespec="seconds"),
        until.isoformat(timespec="seconds"),
    )

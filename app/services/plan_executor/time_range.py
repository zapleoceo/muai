from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.answering_types import PlanTimeRange


@dataclass(frozen=True)
class ResolvedRange:
    from_utc: datetime
    to_utc: datetime


def _parse_explicit(v: str) -> datetime | date:
    s = v.strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return date.fromisoformat(s)


def resolve_time_range(
    *,
    time_range: PlanTimeRange,
    tz: str,
    explicit_from: str | None,
    explicit_to: str | None,
) -> ResolvedRange | None:
    if time_range == PlanTimeRange.NONE:
        return None

    zone = ZoneInfo(tz)
    now_local = datetime.now(tz=zone)
    today_local = now_local.date()

    if time_range == PlanTimeRange.TODAY:
        start_local = datetime.combine(today_local, time(0, 0), tzinfo=zone)
        end_local = start_local + timedelta(days=1)
    elif time_range == PlanTimeRange.YESTERDAY:
        start_local = datetime.combine(today_local - timedelta(days=1), time(0, 0), tzinfo=zone)
        end_local = datetime.combine(today_local, time(0, 0), tzinfo=zone)
    elif time_range == PlanTimeRange.LAST_7_DAYS:
        start_local = datetime.combine(today_local - timedelta(days=6), time(0, 0), tzinfo=zone)
        end_local = datetime.combine(today_local + timedelta(days=1), time(0, 0), tzinfo=zone)
    elif time_range == PlanTimeRange.LAST_30_DAYS:
        start_local = datetime.combine(today_local - timedelta(days=29), time(0, 0), tzinfo=zone)
        end_local = datetime.combine(today_local + timedelta(days=1), time(0, 0), tzinfo=zone)
    elif time_range == PlanTimeRange.ALL_TIME:
        start_local = datetime(1970, 1, 1, 0, 0, tzinfo=zone)
        end_local = now_local + timedelta(seconds=1)
    elif time_range == PlanTimeRange.EXPLICIT:
        if not explicit_from or not explicit_to:
            raise ValueError("explicit_from/explicit_to required")
        a = _parse_explicit(explicit_from)
        b = _parse_explicit(explicit_to)
        if isinstance(a, date) and not isinstance(a, datetime):
            start_local = datetime.combine(a, time(0, 0), tzinfo=zone)
        else:
            start_local = a if isinstance(a, datetime) else datetime.combine(a, time(0, 0), tzinfo=zone)
            if start_local.tzinfo is None:
                start_local = start_local.replace(tzinfo=zone)
        if isinstance(b, date) and not isinstance(b, datetime):
            end_local = datetime.combine(b + timedelta(days=1), time(0, 0), tzinfo=zone)
        else:
            end_local = b if isinstance(b, datetime) else datetime.combine(b, time(0, 0), tzinfo=zone)
            if end_local.tzinfo is None:
                end_local = end_local.replace(tzinfo=zone)
    else:
        raise ValueError("Unsupported time_range")

    return ResolvedRange(
        from_utc=start_local.astimezone(timezone.utc),
        to_utc=end_local.astimezone(timezone.utc),
    )

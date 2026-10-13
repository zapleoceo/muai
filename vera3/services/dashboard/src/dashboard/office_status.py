"""Pure status model for Office cards: one of six statuses plus separate blocker and freshness."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

QUEUE, WORKING, WAITING, REVIEW, DONE, CANCELLED = (
    "Новая / в очереди", "В работе", "Ждёт / зависимость", "Ревью", "Готово", "Отменена")
STATUSES = (QUEUE, WORKING, WAITING, REVIEW, DONE, CANCELLED)

FRESH, STALE, UNKNOWN = "fresh", "stale", "unknown"
FRESH_MAX_AGE = timedelta(hours=1)

_WAITING_RAW = frozenset({"blocked", "waiting", "paused"})


@dataclass(frozen=True)
class StatusInfo:
    status: str
    confirmed_blocker: bool
    reason: str | None
    last_signal_at: datetime | None
    freshness: str


def last_signal_of(row: Any, event_at: datetime | None = None) -> datetime | None:
    """Latest of row.last_progress_at and the latest done/progress/claim event time."""
    times = [t for t in (row.last_progress_at, event_at) if t is not None]
    return max(times) if times else None


def freshness_of(signal_at: datetime | None, now: datetime) -> str:
    if signal_at is None:
        return UNKNOWN
    return FRESH if now - signal_at <= FRESH_MAX_AGE else STALE


def _live_lease(row: Any, now: datetime) -> bool:
    return bool(row.lease_holder and row.lease_until and row.lease_until > now)


def _status(row: Any, now: datetime, *, has_open_question: bool, answered: bool,
            control: str | None) -> str:
    if row.status == "done":
        return DONE
    if row.status == "cancelled":
        return CANCELLED
    if has_open_question:
        return WAITING
    if answered or control == "review":
        return REVIEW
    if control == "paused" or row.status in _WAITING_RAW:
        return WAITING
    return WORKING if _live_lease(row, now) else QUEUE


def describe_status(row: Any, now: datetime, *, has_open_question: bool = False,
                    answered: bool = False, control: str | None = None,
                    event_at: datetime | None = None) -> StatusInfo:
    status = _status(row, now, has_open_question=has_open_question, answered=answered,
                     control=control)
    reason = (row.waiting_reason or row.note or None) if status == WAITING else None
    signal = last_signal_of(row, event_at)
    return StatusInfo(status, has_open_question and status == WAITING, reason, signal,
                      freshness_of(signal, now))

"""Pure status model for Office cards: one of six statuses plus separate blocker and freshness."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

QUEUE, WORKING, BLOCKER, REVIEW, DONE, CANCELLED = (
    "Новая / в очереди", "В работе", "Блокер", "Ревью", "Готово", "Отменена")
STATUSES = (QUEUE, WORKING, BLOCKER, REVIEW, DONE, CANCELLED)

CONFIRMED, UNCONFIRMED = "подтверждён", "не подтверждён"

FRESH, STALE, UNKNOWN = "fresh", "stale", "unknown"
FRESH_MAX_AGE = timedelta(hours=1)

_BLOCKER_RAW = frozenset({"blocked", "waiting", "paused"})
BLOCKER_EVIDENCE_KINDS = frozenset({"blocked", "waiting"})


@dataclass(frozen=True)
class StatusInfo:
    status: str
    blocker_confidence: str | None
    wait_reason: str | None
    dependency: str | None
    open_question: bool
    last_signal_at: datetime | None
    freshness: str


def last_signal_of(row: Any, event_at: datetime | None = None) -> datetime | None:
    """Latest of row.last_progress_at and the latest done/progress event time."""
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
        return BLOCKER
    if answered or control == "review":
        return REVIEW
    if control == "paused" or row.status in _BLOCKER_RAW:
        return BLOCKER
    return WORKING if _live_lease(row, now) else QUEUE


def _actor_ok(row: Any, agent: str | None) -> bool:
    known = {a for a in (row.lease_holder, getattr(row, "owner", None),
                         getattr(row, "responsible", None)) if a}
    return bool(agent) and (not known or agent in known)


def blocker_confidence_of(row: Any, *, has_open_question: bool, control: str | None,
                          control_text: str | None, control_agent: str | None) -> str:
    """Confirmed only by explicit evidence: an open question on the task, or the latest
    control event being blocked/waiting with a non-empty reason from the task's actor."""
    if has_open_question:
        return CONFIRMED
    if (control in BLOCKER_EVIDENCE_KINDS and (control_text or "").strip()
            and _actor_ok(row, control_agent)):
        return CONFIRMED
    return UNCONFIRMED


def dependency_of(row: Any) -> str | None:
    parts = []
    deps = getattr(row, "depends_on", None)
    if deps:
        parts.append("зависит от: " + ", ".join(str(d) for d in deps))
    until = getattr(row, "waiting_until", None)
    if until is not None:
        parts.append(f"ждём до {until:%Y-%m-%d %H:%M}")
    return "; ".join(parts) or None


def describe_status(row: Any, now: datetime, *, has_open_question: bool = False,
                    answered: bool = False, control: str | None = None,
                    event_at: datetime | None = None, control_text: str | None = None,
                    control_agent: str | None = None) -> StatusInfo:
    status = _status(row, now, has_open_question=has_open_question, answered=answered,
                     control=control)
    blocked = status == BLOCKER
    confidence = (blocker_confidence_of(row, has_open_question=has_open_question,
                                        control=control, control_text=control_text,
                                        control_agent=control_agent) if blocked else None)
    wait_reason = (getattr(row, "waiting_reason", None) or row.note or None) if blocked else None
    signal = last_signal_of(row, event_at)
    return StatusInfo(status, confidence, wait_reason, dependency_of(row) if blocked else None,
                      has_open_question and status not in (DONE, CANCELLED), signal,
                      freshness_of(signal, now))

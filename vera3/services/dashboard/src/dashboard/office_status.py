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
BLOCKER_EVIDENCE_KINDS = frozenset({"blocked", "released", "waiting"})


@dataclass(frozen=True)
class StatusInfo:
    status: str
    blocker_confidence: str | None
    wait_reason: str | None
    dependency: str | None
    open_question: bool
    last_signal_at: datetime | None
    freshness: str


def _last_signal(row: Any, event_at: datetime | None) -> datetime | None:
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


def _known_actors(row: Any, data: dict[str, Any] | None) -> set[str]:
    candidates = (row.lease_holder, getattr(row, "created_by", None),
                  getattr(row, "owner", None), getattr(row, "responsible", None),
                  (data or {}).get("holder"))
    return {a for a in candidates if a}


BOUND_AT_TOLERANCE = timedelta(seconds=1)


def bound_reason_of(blocked: Any, following: Any) -> str | None:
    """Reason of a `blocked` flip, taken only from the progress event of the same update call.

    update(status="blocked") writes the flip (no text) and then, in one transaction, a
    progress event with text=note and data={"status": "blocked"}. The pair is the next event
    of the task by id, same agent and fencing token, same `at` (within BOUND_AT_TOLERANCE).
    Mutable row fields (note, last_progress_text) are never used: later progress or an older
    note would otherwise "confirm" the blocker.
    """
    if blocked is None or following is None or blocked.kind != "blocked":
        return None
    same_call = (following.kind == "progress" and following.id > blocked.id
                 and following.agent == blocked.agent
                 and following.fencing_token == blocked.fencing_token
                 and (following.data or {}).get("status") == "blocked"
                 and abs(following.at - blocked.at) <= BOUND_AT_TOLERANCE)
    return (following.text or "").strip() or None if same_call else None


def _event_reason(control: str | None, text: str | None, data: dict[str, Any] | None,
                  bound_reason: str | None) -> str:
    """Reason carried by the control event in the shapes vera_shared.room writes.

    `waiting` (task_wait) and `released` with data.status == "blocked" (release) carry it in
    the event text; the `blocked` flip carries it through bound_reason_of.
    """
    if control not in BLOCKER_EVIDENCE_KINDS:
        return ""
    if control == "released" and (data or {}).get("status") != "blocked":
        return ""
    return ((text or bound_reason or "") if control == "blocked" else (text or "")).strip()


def blocker_confidence_of(row: Any, *, has_open_question: bool, control: str | None,
                          control_text: str | None, control_agent: str | None,
                          control_data: dict[str, Any] | None = None,
                          control_bound_reason: str | None = None) -> str:
    """Confirmed only by explicit evidence: an open question on the task, or the latest
    control event being blocked/released(blocked)/waiting with a non-empty reason whose
    author is a known actor of the task (holder, created_by, owner, responsible).
    With no known actor at all, any non-empty author is accepted."""
    if has_open_question:
        return CONFIRMED
    if control not in BLOCKER_EVIDENCE_KINDS or not control_agent:
        return UNCONFIRMED
    if not _event_reason(control, control_text, control_data, control_bound_reason):
        return UNCONFIRMED
    known = _known_actors(row, control_data)
    return CONFIRMED if not known or control_agent in known else UNCONFIRMED


def _dependency(row: Any, now: datetime, active: bool) -> str | None:
    parts = []
    deps = getattr(row, "depends_on", None)
    if deps and active:
        parts.append("зависит от: " + ", ".join(str(d) for d in deps))
    until = getattr(row, "waiting_until", None)
    if until is not None and until > now:
        parts.append(f"ждём до {until:%Y-%m-%d %H:%M}")
    return "; ".join(parts) or None


def describe_status(row: Any, now: datetime, *, has_open_question: bool = False,
                    answered: bool = False, control: str | None = None,
                    event_at: datetime | None = None, control_text: str | None = None,
                    control_agent: str | None = None,
                    control_data: dict[str, Any] | None = None,
                    control_bound_reason: str | None = None) -> StatusInfo:
    status = _status(row, now, has_open_question=has_open_question, answered=answered,
                     control=control)
    blocked = status == BLOCKER
    confidence = (blocker_confidence_of(row, has_open_question=has_open_question,
                                        control=control, control_text=control_text,
                                        control_agent=control_agent,
                                        control_data=control_data,
                                        control_bound_reason=control_bound_reason)
                  if blocked else None)
    open_task = status not in (DONE, CANCELLED)
    waiting_reason = getattr(row, "waiting_reason", None) if open_task else None
    event_reason = _event_reason(control, control_text, control_data, control_bound_reason)
    wait_reason = waiting_reason or (event_reason or None if blocked else None)
    waiting_active = bool(waiting_reason) or (
        getattr(row, "waiting_until", None) is not None and row.waiting_until > now)
    dependency = _dependency(row, now, blocked or waiting_active) if open_task else None
    signal = _last_signal(row, event_at)
    return StatusInfo(status, confidence, wait_reason, dependency,
                      has_open_question and open_task, signal, freshness_of(signal, now))

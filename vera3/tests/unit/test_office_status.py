"""Table-driven checks of the pure Office status model."""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dashboard.office_status import (
    CANCELLED,
    DONE,
    FRESH,
    FRESH_MAX_AGE,
    QUEUE,
    REVIEW,
    STALE,
    STATUSES,
    UNKNOWN,
    WAITING,
    WORKING,
    describe_status,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
LEASES = {"live": ("dot", NOW + timedelta(minutes=5)),
          "expired": ("dot", NOW - timedelta(minutes=5)),
          "none": (None, None)}
AGES = {"none": None, "fresh": NOW - timedelta(minutes=10), "stale": NOW - timedelta(hours=3)}
FRESHNESS = {"none": UNKNOWN, "fresh": FRESH, "stale": STALE}


def row(status: str, lease: str, progress_at: datetime | None) -> SimpleNamespace:
    holder, until = LEASES[lease]
    return SimpleNamespace(status=status, lease_holder=holder, lease_until=until,
                           last_progress_at=progress_at, waiting_reason="ждём API",
                           note="заметка")


def expected_status(raw: str, question: bool, lease: str, control: str | None) -> str:
    if raw == "done":
        return DONE
    if raw == "cancelled":
        return CANCELLED
    if question:
        return WAITING
    if control == "review":
        return REVIEW
    if raw in ("blocked", "waiting", "paused"):
        return WAITING
    return WORKING if lease == "live" else QUEUE


CASES = list(itertools.product(
    ("open", "claimed", "blocked", "waiting", "paused", "done", "cancelled"),
    (False, True), tuple(LEASES), tuple(AGES), (None, "review")))


@pytest.mark.parametrize(("raw", "question", "lease", "age", "control"), CASES)
def test_matrix(raw: str, question: bool, lease: str, age: str, control: str | None) -> None:
    info = describe_status(row(raw, lease, AGES[age]), NOW, has_open_question=question,
                           control=control)
    assert info.status in STATUSES
    assert info.status == expected_status(raw, question, lease, control)
    assert info.confirmed_blocker == (question and info.status == WAITING)
    assert info.last_signal_at == AGES[age]
    assert info.freshness == FRESHNESS[age]
    assert (info.reason == "ждём API") == (info.status == WAITING)


def test_blocked_without_question_is_waiting_not_confirmed_blocker() -> None:
    info = describe_status(row("blocked", "none", None), NOW)
    assert (info.status, info.confirmed_blocker, info.reason) == (WAITING, False, "ждём API")


def test_waiting_reason_falls_back_to_note() -> None:
    r = row("blocked", "none", None)
    r.waiting_reason = None
    assert describe_status(r, NOW).reason == "заметка"


def test_answered_and_pause_control() -> None:
    assert describe_status(row("open", "live", None), NOW, answered=True).status == REVIEW
    assert describe_status(row("open", "live", None), NOW, control="paused").status == WAITING


def test_event_time_counts_as_signal_and_boundary_is_fresh() -> None:
    r = row("open", "live", NOW - timedelta(hours=5))
    info = describe_status(r, NOW, event_at=NOW - FRESH_MAX_AGE)
    assert info.last_signal_at == NOW - FRESH_MAX_AGE
    assert info.freshness == FRESH
    late = describe_status(r, NOW, event_at=NOW - FRESH_MAX_AGE - timedelta(seconds=1))
    assert late.freshness == STALE

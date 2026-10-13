"""Table-driven checks of the pure Office status model."""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dashboard.office_status import (
    BLOCKER,
    CANCELLED,
    CONFIRMED,
    DONE,
    FRESH,
    FRESH_MAX_AGE,
    QUEUE,
    REVIEW,
    STALE,
    STATUSES,
    UNCONFIRMED,
    UNKNOWN,
    WORKING,
    describe_status,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
LEASES = {"live": ("dot", NOW + timedelta(minutes=5)),
          "expired": ("dot", NOW - timedelta(minutes=5)),
          "none": (None, None)}
AGES = {"none": None, "fresh": NOW - timedelta(minutes=10), "stale": NOW - timedelta(hours=3)}
FRESHNESS = {"none": UNKNOWN, "fresh": FRESH, "stale": STALE}
BLOCKER_RAW = ("blocked", "waiting", "paused")


def row(status: str, lease: str, progress_at: datetime | None) -> SimpleNamespace:
    holder, until = LEASES[lease]
    return SimpleNamespace(status=status, lease_holder=holder, lease_until=until,
                           last_progress_at=progress_at, waiting_reason="ждём API",
                           note="заметка", owner=None, responsible=None,
                           depends_on=[], waiting_until=None)


def expected_status(raw: str, question: bool, lease: str, control: str | None) -> str:
    if raw == "done":
        return DONE
    if raw == "cancelled":
        return CANCELLED
    if question:
        return BLOCKER
    if control == "review":
        return REVIEW
    if raw in BLOCKER_RAW:
        return BLOCKER
    return WORKING if lease == "live" else QUEUE


CASES = list(itertools.product(
    ("open", "claimed", *BLOCKER_RAW, "done", "cancelled"),
    (False, True), tuple(LEASES), tuple(AGES), (None, "review")))


@pytest.mark.parametrize(("raw", "question", "lease", "age", "control"), CASES)
def test_matrix(raw: str, question: bool, lease: str, age: str, control: str | None) -> None:
    info = describe_status(row(raw, lease, AGES[age]), NOW, has_open_question=question,
                           control=control)
    assert info.status in STATUSES
    assert "Ждёт" not in info.status
    assert info.status == expected_status(raw, question, lease, control)
    assert (info.blocker_confidence is not None) == (info.status == BLOCKER)
    if info.status == BLOCKER:
        assert info.blocker_confidence == (CONFIRMED if question else UNCONFIRMED)
        assert info.wait_reason == "ждём API"
    else:
        assert info.wait_reason is None
    assert info.open_question == (question and raw not in ("done", "cancelled"))
    assert info.last_signal_at == AGES[age]
    assert info.freshness == FRESHNESS[age]


EVIDENCE = [
    ("blocked", "причина", "dot", CONFIRMED),
    ("waiting", "ждём ключ", "dot", CONFIRMED),
    ("blocked", "", "dot", UNCONFIRMED),
    ("blocked", "   ", "dot", UNCONFIRMED),
    ("blocked", None, "dot", UNCONFIRMED),
    ("blocked", "причина", "other", UNCONFIRMED),
    ("blocked", "причина", None, UNCONFIRMED),
    ("paused", "причина", "dot", UNCONFIRMED),
    ("claimed", "причина", "dot", UNCONFIRMED),
    (None, None, None, UNCONFIRMED),
]


@pytest.mark.parametrize(("kind", "text", "agent", "expected"), EVIDENCE)
def test_blocker_confidence_from_control_event(kind: str | None, text: str | None,
                                               agent: str | None, expected: str) -> None:
    info = describe_status(row("blocked", "live", None), NOW, control=kind,
                           control_text=text, control_agent=agent)
    assert info.status == BLOCKER
    assert info.blocker_confidence == expected


def test_unconfirmed_blocker_without_question() -> None:
    info = describe_status(row("blocked", "none", None), NOW)
    assert (info.status, info.blocker_confidence, info.open_question) == (
        BLOCKER, UNCONFIRMED, False)


def test_open_question_on_working_task_is_blocker_and_reported() -> None:
    info = describe_status(row("claimed", "live", AGES["fresh"]), NOW, has_open_question=True)
    assert (info.status, info.blocker_confidence, info.open_question) == (
        BLOCKER, CONFIRMED, True)


def test_wait_reason_falls_back_to_note_and_dependency() -> None:
    r = row("blocked", "none", None)
    r.waiting_reason = None
    r.depends_on = ["T-1", "T-2"]
    r.waiting_until = datetime(2026, 10, 15, 9, 30)
    info = describe_status(r, NOW)
    assert info.wait_reason == "заметка"
    assert info.dependency == "зависит от: T-1, T-2; ждём до 2026-10-15 09:30"
    assert describe_status(row("blocked", "none", None), NOW).dependency is None


def test_answered_and_pause_control() -> None:
    assert describe_status(row("open", "live", None), NOW, answered=True).status == REVIEW
    paused = describe_status(row("open", "live", None), NOW, control="paused")
    assert (paused.status, paused.blocker_confidence) == (BLOCKER, UNCONFIRMED)


def test_event_time_counts_as_signal_and_boundary_is_fresh() -> None:
    r = row("open", "live", NOW - timedelta(hours=5))
    info = describe_status(r, NOW, event_at=NOW - FRESH_MAX_AGE)
    assert info.last_signal_at == NOW - FRESH_MAX_AGE
    assert info.freshness == FRESH
    late = describe_status(r, NOW, event_at=NOW - FRESH_MAX_AGE - timedelta(seconds=1))
    assert late.freshness == STALE

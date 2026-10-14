"""Checks of the pure Office status model on an explicit expected table."""
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
    bound_reason_of,
    describe_status,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
LEASES = {"live": ("dot", NOW + timedelta(minutes=5)),
          "expired": ("dot", NOW - timedelta(minutes=5)),
          "none": (None, None)}
AGES = {"none": None, "fresh": NOW - timedelta(minutes=10), "stale": NOW - timedelta(hours=3)}
FRESHNESS = {"none": UNKNOWN, "fresh": FRESH, "stale": STALE}


def row(status: str, lease: str = "none", progress_at: datetime | None = None,
        **extra: object) -> SimpleNamespace:
    holder, until = LEASES[lease]
    fields = {"status": status, "lease_holder": holder, "lease_until": until,
              "last_progress_at": progress_at, "waiting_reason": None, "note": "заметка",
              "last_progress_text": None,
              "owner": None, "responsible": None, "created_by": "dot", "depends_on": [],
              "waiting_until": None}
    return SimpleNamespace(**{**fields, **extra})


# (raw status, lease, open question, answered, control) -> expected status
EXPECTED = [
    ("open", "none", False, False, None, QUEUE),
    ("open", "live", False, False, None, WORKING),
    ("in_progress", "live", False, False, None, WORKING),
    ("in_progress", "expired", False, False, None, QUEUE),
    ("blocked", "live", False, False, None, BLOCKER),
    ("blocked", "none", False, False, None, BLOCKER),
    ("waiting", "none", False, False, None, QUEUE),
    ("waiting", "live", False, False, None, WORKING),
    ("paused", "none", False, False, None, BLOCKER),
    ("in_progress", "live", False, False, "paused", BLOCKER),
    ("in_progress", "live", True, False, None, BLOCKER),
    ("open", "none", True, False, None, BLOCKER),
    ("in_progress", "live", False, False, "review", REVIEW),
    ("in_progress", "live", False, True, None, REVIEW),
    ("blocked", "none", False, True, None, REVIEW),
    ("blocked", "none", False, False, "review", REVIEW),
    ("blocked", "none", True, True, "review", BLOCKER),
    ("in_progress", "live", False, False, "waiting", WORKING),
    ("in_progress", "live", False, False, "blocked", WORKING),
    ("done", "none", False, False, None, DONE),
    ("done", "live", True, True, "review", DONE),
    ("cancelled", "none", False, False, None, CANCELLED),
    ("cancelled", "live", True, True, "paused", CANCELLED),
]


@pytest.mark.parametrize(("raw", "lease", "question", "answered", "control", "expected"),
                         EXPECTED)
def test_expected_status_table(raw: str, lease: str, question: bool, answered: bool,
                               control: str | None, expected: str) -> None:
    info = describe_status(row(raw, lease), NOW, has_open_question=question,
                           answered=answered, control=control)
    assert info.status == expected


MATRIX = list(itertools.product(
    ("open", "in_progress", "blocked", "waiting", "paused", "done", "cancelled"),
    (False, True), tuple(LEASES), tuple(AGES), (None, "review", "paused"), (False, True)))


@pytest.mark.parametrize(("raw", "question", "lease", "age", "control", "answered"), MATRIX)
def test_invariants(raw: str, question: bool, lease: str, age: str, control: str | None,
                    answered: bool) -> None:
    info = describe_status(row(raw, lease, AGES[age]), NOW, has_open_question=question,
                           control=control, answered=answered)
    assert info.status in STATUSES
    assert "Ждёт" not in info.status
    assert (info.blocker_confidence is not None) == (info.status == BLOCKER)
    if question and info.status == BLOCKER:
        assert info.blocker_confidence == CONFIRMED
    if info.status in (DONE, CANCELLED):
        assert (info.open_question, info.wait_reason, info.dependency) == (False, None, None)
    assert info.open_question == (question and raw not in ("done", "cancelled"))
    assert info.last_signal_at == AGES[age]
    assert info.freshness == FRESHNESS[age]


EVIDENCE = [
    ("blocked", "причина", "dot", None, CONFIRMED),
    ("waiting", "ждём ключ", "dot", None, CONFIRMED),
    ("released", "причина", "dot", {"status": "blocked"}, CONFIRMED),
    ("released", "причина", "dot", {"status": "open"}, UNCONFIRMED),
    ("released", "", "dot", {"status": "blocked"}, UNCONFIRMED),
    ("blocked", "", "dot", None, UNCONFIRMED),
    ("blocked", "причина", "other", None, UNCONFIRMED),
    ("blocked", "причина", None, None, UNCONFIRMED),
    ("paused", "причина", "dot", None, UNCONFIRMED),
    ("claimed", "причина", "dot", None, UNCONFIRMED),
    (None, None, None, None, UNCONFIRMED),
]


@pytest.mark.parametrize(("kind", "text", "agent", "data", "expected"), EVIDENCE)
def test_blocker_confidence_from_control_event(kind: str | None, text: str | None,
                                               agent: str | None, data: dict | None,
                                               expected: str) -> None:
    info = describe_status(row("blocked", "none"), NOW, control=kind, control_text=text,
                           control_agent=agent, control_data=data)
    assert info.status == BLOCKER
    assert info.blocker_confidence == expected


def event(kind: str, id_: int, **extra: object) -> SimpleNamespace:
    fields = {"kind": kind, "id": id_, "agent": "dot", "fencing_token": 1, "at": NOW,
              "text": None, "data": None}
    return SimpleNamespace(**{**fields, **extra})


def test_bound_reason_requires_the_paired_progress_event() -> None:
    flip = event("blocked", 5, data={"status": "blocked"})
    paired = event("progress", 6, text="ждём ключ", data={"status": "blocked"})
    assert bound_reason_of(flip, paired) == "ждём ключ"
    assert bound_reason_of(flip, event("progress", 6, text="  ")) is None
    unrelated = [
        event("progress", 6, text="x", data=None),
        event("progress", 6, text="x", data={"status": "in_progress"}),
        event("progress", 6, text="x", data={"status": "blocked"}, agent="other"),
        event("progress", 6, text="x", data={"status": "blocked"}, fencing_token=2),
        event("progress", 6, text="x", data={"status": "blocked"}, at=NOW + timedelta(minutes=5)),
        event("progress", 4, text="x", data={"status": "blocked"}),
        event("heartbeat", 6, text="x", data={"status": "blocked"}),
    ]
    assert [bound_reason_of(flip, e) for e in unrelated] == [None] * len(unrelated)
    assert bound_reason_of(flip, None) is None
    assert bound_reason_of(event("released", 5), paired) is None


def test_blocked_flip_confirmed_only_by_bound_reason_not_row_fields() -> None:
    reasoned = describe_status(row("blocked"), NOW, control="blocked", control_agent="dot",
                               control_bound_reason="ждём ключ")
    assert reasoned.blocker_confidence == CONFIRMED
    assert reasoned.wait_reason == "ждём ключ"
    stale = describe_status(
        row("blocked", note="старая", last_progress_text="посторонний прогресс",
            waiting_reason=None), NOW, control="blocked", control_agent="dot")
    assert stale.blocker_confidence == UNCONFIRMED
    assert stale.wait_reason is None


def test_actor_must_be_a_known_actor_of_the_task() -> None:
    cleared = row("blocked", created_by="vera", owner="boss", responsible="dev")
    for agent, expected in (("vera", CONFIRMED), ("boss", CONFIRMED), ("dev", CONFIRMED),
                            ("stranger", UNCONFIRMED)):
        info = describe_status(cleared, NOW, control="released", control_text="причина",
                               control_agent=agent, control_data={"status": "blocked"})
        assert info.blocker_confidence == expected
    previous = describe_status(cleared, NOW, control="released", control_text="причина",
                               control_agent="prev",
                               control_data={"status": "blocked", "holder": "prev"})
    assert previous.blocker_confidence == CONFIRMED


def test_unconfirmed_blocker_without_question() -> None:
    info = describe_status(row("blocked"), NOW)
    assert (info.status, info.blocker_confidence, info.open_question) == (
        BLOCKER, UNCONFIRMED, False)


def test_open_question_on_working_task_is_blocker_and_reported() -> None:
    info = describe_status(row("in_progress", "live", AGES["fresh"]), NOW,
                           has_open_question=True)
    assert (info.status, info.blocker_confidence, info.open_question) == (
        BLOCKER, CONFIRMED, True)


def test_wait_fields_are_independent_of_status() -> None:
    working = row("in_progress", "live", waiting_reason="ждём банк",
                  waiting_until=NOW + timedelta(hours=2), depends_on=["T-1"])
    info = describe_status(working, NOW)
    assert (info.status, info.blocker_confidence) == (WORKING, None)
    assert info.wait_reason == "ждём банк"
    assert info.dependency == "зависит от: T-1; ждём до 2026-10-14 14:00"
    elapsed = row("in_progress", "live", waiting_until=NOW - timedelta(minutes=1))
    quiet = describe_status(elapsed, NOW)
    assert (quiet.wait_reason, quiet.dependency) == (None, None)
    only_until = describe_status(row("open", waiting_until=NOW + timedelta(hours=1)), NOW)
    assert only_until.dependency == "ждём до 2026-10-14 13:00"


def test_wait_reason_for_blocker_never_uses_row_note() -> None:
    info = describe_status(row("blocked", depends_on=["T-1", "T-2"]), NOW)
    assert info.wait_reason is None
    assert info.dependency == "зависит от: T-1, T-2"
    assert describe_status(row("open"), NOW).wait_reason is None


def test_event_time_counts_as_signal_and_boundary_is_fresh() -> None:
    r = row("open", "live", NOW - timedelta(hours=5))
    info = describe_status(r, NOW, event_at=NOW - FRESH_MAX_AGE)
    assert info.last_signal_at == NOW - FRESH_MAX_AGE
    assert info.freshness == FRESH
    late = describe_status(r, NOW, event_at=NOW - FRESH_MAX_AGE - timedelta(seconds=1))
    assert late.freshness == STALE


def test_expired_lease_with_wait_reason_is_waiting_not_blocker() -> None:
    waiting = row("in_progress", "expired", waiting_reason="ждём ответ по трём вопросам",
                  waiting_until=NOW - timedelta(hours=5))
    info = describe_status(waiting, NOW, control="waiting", control_agent="dot",
                           control_text="ждём ответ по трём вопросам",
                           control_data={"until_seconds": 3600},
                           control_at=NOW - timedelta(hours=6))
    assert info.status == QUEUE
    assert info.waiting is True
    assert info.wait_reason == "ждём ответ по трём вопросам"
    assert info.decision_owner is None
    assert info.wait_freshness == STALE
    assert info.blocker_confidence is None


def test_decision_owner_only_from_explicit_event_data() -> None:
    base = row("in_progress", "expired", waiting_reason="нужен live QA")
    guessed = describe_status(base, NOW, control="waiting", control_agent="dot",
                              control_text="нужен live QA", control_data={})
    assert guessed.decision_owner is None
    explicit = describe_status(
        row("in_progress", "live", waiting_reason="решение"), NOW, control="waiting",
        control_agent="dot", control_text="решение",
        control_data={"decision_owner": " owner "}, control_at=NOW - timedelta(minutes=5))
    assert explicit.status == WORKING
    assert explicit.decision_owner == "owner"
    assert explicit.wait_freshness == FRESH

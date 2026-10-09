"""Трекер задач, шаг 2b: attention — чистая функция, время подставляется аргументом."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from vera_shared.room import attention as att
from vera_shared.room.attention import attention, humanize

NOW = datetime(2026, 10, 9, 12, 0)
H = timedelta(hours=1)


def task(**kw):
    base = {"status": "in_progress", "lease_holder": "claude", "lease_until": NOW + H,
            "last_progress_at": None, "last_progress_text": None, "next_checkpoint_at": None,
            "waiting_until": None, "updated_at": NOW - 5 * H}
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize(("delta", "text"), [
    (timedelta(seconds=20), "менее минуты"), (timedelta(minutes=7), "7 мин"),
    (3 * H, "3 ч"), (timedelta(hours=2, minutes=15), "2 ч 15 мин"),
    (timedelta(days=1, hours=4), "1 дн 4 ч"), (timedelta(days=2), "2 дн")])
def test_humanize(delta, text):
    assert humanize(delta) == text


def test_abandoned_open_task_is_unassigned():
    a = attention(task(status="open", lease_holder=None, lease_until=None), NOW)
    assert a.state == att.UNASSIGNED and a.label_ru == "никто не взял 5 ч"
    assert a.since == NOW - 5 * H


def test_expired_lease_with_holder():
    a = attention(task(lease_until=NOW - 3 * H), NOW)
    assert (a.state, a.label_ru) == (att.LEASE_EXPIRED, "аренда истекла 3 ч назад")


def test_live_heartbeat_without_result_past_checkpoint_is_stale():
    t = task(last_progress_at=NOW - 6 * H, last_progress_text="step 1",
             next_checkpoint_at=NOW - 3 * H, updated_at=NOW)
    a = attention(t, NOW)
    assert a.state == att.STALE_PROGRESS and a.label_ru == "нет обновления 6 ч"
    assert a.since == NOW - 3 * H and a.last_result_text == "step 1"
    assert "забыт" not in a.label_ru


def test_before_checkpoint_is_in_progress():
    t = task(last_progress_at=NOW - H, next_checkpoint_at=NOW + H)
    assert attention(t, NOW).state == att.IN_PROGRESS


def test_checkpoint_older_than_last_progress_is_ignored():
    t = task(last_progress_at=NOW - timedelta(minutes=10), next_checkpoint_at=NOW - 3 * H)
    assert attention(t, NOW).state == att.IN_PROGRESS


@pytest.mark.parametrize(("age", "state"), [
    (timedelta(minutes=119), att.IN_PROGRESS), (timedelta(minutes=121), att.STALE_PROGRESS)])
def test_default_window_without_checkpoint(age, state):
    assert attention(task(last_progress_at=NOW - age), NOW).state == state


def test_no_progress_uses_claim_time_and_unknown_baseline_is_not_stale():
    assert attention(task(), NOW, claimed_at=NOW - 3 * H).state == att.STALE_PROGRESS
    assert attention(task(), NOW).state == att.IN_PROGRESS


def test_waiting_until_future_then_stale_after_deadline():
    t = task(waiting_until=NOW + 2 * H, last_progress_at=NOW - 9 * H)
    waiting = attention(t, NOW)
    assert waiting.state == att.WAITING and "ещё 2 ч" in waiting.label_ru
    t.lease_until = NOW + 9 * H
    late = attention(t, NOW + 3 * H)
    assert late.state == att.STALE_PROGRESS and late.label_ru == "срок ожидания вышел 1 ч назад"


def test_heartbeat_and_updated_at_do_not_hide_staleness():
    t = task(last_progress_at=NOW - 5 * H, updated_at=NOW, lease_until=NOW + 4 * H)
    assert attention(t, NOW).state == att.STALE_PROGRESS


def test_terminal_and_explicit_states():
    assert attention(task(status="done"), NOW).state == att.DONE
    assert attention(task(status="cancelled"), NOW).state == att.CANCELLED
    assert attention(task(), NOW, paused=True).state == att.PAUSED
    assert attention(task(status="blocked"), NOW).state == att.BLOCKED
    asked = attention(task(), NOW, open_question_at=NOW - 2 * H)
    assert (asked.state, asked.since) == (att.NEEDS_OWNER, NOW - 2 * H)

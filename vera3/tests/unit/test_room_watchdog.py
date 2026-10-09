"""Трекер задач, шаг 6: сторож (решения при подставном времени, дедупликация, состояние)."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow, WatchdogStateRow
from vera_shared.room import messages, task_progress, tasks, watchdog
from vera_shared.room.tasks import StaleLease
from vera_shared.room.watchdog_rules import MIN_LEASE, lease_length
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio


async def kinds(task_id: str) -> list[str]:
    evs = await task_progress.history(room="main", task_id=task_id, since_id=None, limit=100)
    return [e["kind"] for e in evs]


async def row_of(task_id: str) -> RoomTaskRow:
    async with get_session() as s:
        return await s.get(RoomTaskRow, ("main", task_id))


async def claimed(task_id: str) -> None:
    await tasks.claim(room="main", task_id=task_id, agent="claude", lease_seconds=900)


async def test_expired_lease_without_progress_is_reopened_once(sqlite_db):
    await claimed("W1")
    late = utc_naive_now() + timedelta(hours=3)
    first = await watchdog.run_once(late)
    assert first["actions"] == [("main", "W1", "lease_expired"),
                                ("main", "W1", "watchdog_action")]
    r = await row_of("W1")
    assert (r.status, r.lease_holder, r.fencing_token) == ("open", None, 1)
    again = await watchdog.run_once(late + timedelta(minutes=1))
    assert again["actions"] == []
    assert (await kinds("W1")).count("lease_expired") == 1
    with pytest.raises(StaleLease):
        await tasks.update(room="main", task_id="W1", agent="claude", fencing_token=1,
                           note="проснулся")


async def test_expired_lease_with_recent_progress_is_escalated_not_reopened(sqlite_db):
    await claimed("W2")
    now = utc_naive_now()
    await tasks.update(room="main", task_id="W2", agent="claude", fencing_token=1,
                       note="делаю")
    late = now + timedelta(minutes=20)  # аренда (15 мин) вышла, прогресс свежий
    assert (await watchdog.run_once(late))["actions"] == [("main", "W2", "lease_expired")]
    assert (await watchdog.run_once(late + timedelta(minutes=1)))["actions"] == []
    r = await row_of("W2")
    assert (r.status, r.lease_holder) == ("in_progress", "claude")
    assert (await kinds("W2")).count("lease_expired") == 1
    # позже порог 2x длины аренды пройден — тогда задача возвращается в open
    later = now + timedelta(hours=3)
    assert (await watchdog.run_once(later))["actions"] == [("main", "W2", "watchdog_action")]
    assert (await row_of("W2")).status == "open"


async def test_paused_and_done_tasks_are_untouched(sqlite_db):
    await claimed("P1")
    await task_progress.set_state(room="main", task_id="P1", agent="claude", fencing_token=1,
                                  state="paused", reason="обед")
    await claimed("D1")
    await tasks.release(room="main", task_id="D1", agent="claude", fencing_token=1,
                        status="done")
    out = await watchdog.run_once(utc_naive_now() + timedelta(days=2))
    assert out["actions"] == []
    assert (await row_of("P1")).lease_holder == "claude"
    assert "lease_expired" not in await kinds("P1")


async def test_overdue_checkpoint_notifies_once_per_checkpoint(sqlite_db):
    await claimed("C1")
    await task_progress.progress(room="main", task_id="C1", agent="claude", fencing_token=1,
                                 result="старт", next_checkpoint_seconds=120)
    await tasks.update(room="main", task_id="C1", agent="claude", fencing_token=1,
                       extend_seconds=3600)
    late = utc_naive_now() + timedelta(minutes=10)
    assert (await watchdog.run_once(late))["actions"] == [("main", "C1", "watchdog_action")]
    assert (await watchdog.run_once(late + timedelta(minutes=1)))["actions"] == []
    items, _ = await messages.inbox(agent="claude", room="main", consumer="default",
                                    limit=10, since_id=None)
    assert len(items) == 1 and items[0]["from"] == "watchdog" and items[0]["task_id"] == "C1"
    assert (await kinds("C1")).count("watchdog_action") == 1
    # новый прогресс и новая просроченная точка — новое уведомление
    await task_progress.progress(room="main", task_id="C1", agent="claude", fencing_token=1,
                                 result="ещё", next_checkpoint_seconds=60)
    again = await watchdog.run_once(utc_naive_now() + timedelta(minutes=10))
    assert again["actions"] == [("main", "C1", "watchdog_action")]


async def test_run_writes_state_and_records_task_errors(sqlite_db, monkeypatch):
    await claimed("E1")
    now = utc_naive_now()
    await watchdog.run_once(now)
    async with get_session() as s:
        st = await s.get(WatchdogStateRow, "room")
    assert (st.last_run_at, st.last_error) == (now, None)

    async def boom(*a, **k):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(watchdog, "check_task", boom)
    out = await watchdog.run_once(now + timedelta(minutes=1))
    assert "db is on fire" in out["error"]
    async with get_session() as s:
        st = await s.get(WatchdogStateRow, "room")
    assert st.last_run_at == now + timedelta(minutes=1) and "db is on fire" in st.last_error


async def test_lease_length_has_a_floor():
    now = utc_naive_now()
    assert lease_length(now + timedelta(seconds=60), now) == MIN_LEASE
    assert lease_length(now + timedelta(hours=1), now) == timedelta(hours=1)
    assert lease_length(now, None) == MIN_LEASE


async def test_stale_indicator_is_red_when_old_or_errored():
    from dashboard.watchdog_view import watchdog_badge
    now = utc_naive_now()
    ok = SimpleNamespace(last_run_at=now - timedelta(seconds=30), last_error=None)
    assert "ok" in watchdog_badge(ok, now) and "30 с назад" in watchdog_badge(ok, now)
    old = SimpleNamespace(last_run_at=now - timedelta(minutes=5), last_error=None)
    err = SimpleNamespace(last_run_at=now, last_error="<b>x</b>")
    assert "warn" in watchdog_badge(old, now)
    assert "warn" in watchdog_badge(err, now) and "<b>" not in watchdog_badge(err, now)
    assert "warn" in watchdog_badge(None, now)


async def set_row(task_id: str, **fields) -> None:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", task_id))
        for k, v in fields.items():
            setattr(row, k, v)


async def test_reclaim_after_old_progress_is_not_reopened(sqlite_db):
    now = utc_naive_now()
    await claimed("R1")
    await set_row("R1", last_progress_at=now - timedelta(hours=5),
                  lease_until=now - timedelta(hours=1))
    await tasks.claim(room="main", task_id="R1", agent="codex", lease_seconds=900)
    out = await watchdog.run_once(now + timedelta(minutes=20))
    assert out["actions"] == [("main", "R1", "lease_expired")]
    assert (await row_of("R1")).lease_holder == "codex"


async def test_handoff_accept_with_old_progress_is_escalated_not_reopened(sqlite_db):
    from vera_shared.room import handoff
    now = utc_naive_now()
    await claimed("R2")
    await set_row("R2", last_progress_at=now - timedelta(hours=5))
    await handoff.offer(room="main", task_id="R2", agent="claude", fencing_token=1,
                        to_agent="codex")
    await handoff.accept(room="main", task_id="R2", agent="codex")
    out = await watchdog.run_once(now + timedelta(minutes=20))
    assert out["actions"] == [("main", "R2", "lease_expired")]
    assert (await row_of("R2")).status == "in_progress"


async def test_state_write_failure_is_logged(sqlite_db, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("no disk")

    monkeypatch.setattr(watchdog, "write_state", boom)
    with caplog.at_level("WARNING"):
        await watchdog.run_once(utc_naive_now())
    assert "cannot write watchdog_state" in caplog.text and "no disk" in caplog.text


async def test_failing_task_is_logged_by_id(sqlite_db, monkeypatch, caplog):
    await claimed("L1")

    async def boom(*a, **k):
        raise RuntimeError("bad row")

    monkeypatch.setattr(watchdog, "check_task", boom)
    with caplog.at_level("WARNING"):
        await watchdog.run_once(utc_naive_now())
    assert "main/L1" in caplog.text and "bad row" in caplog.text

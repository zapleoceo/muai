"""Зависшие задачи: правило, поиск и действия владельца, которые их двигают."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room import (
    owner_actions,
    owner_task_actions,
    questions,
    task_progress,
    tasks,
)
from vera_shared.room.stall import stalled_tasks
from vera_shared.room.stall_rules import (
    STALL_AFTER,
    StallFacts,
    days_word,
    is_stalled,
    question_text,
)
from vera_shared.room.tasks import TaskBusy
from vera_shared.timeutil import utc_naive_now

R = "main"
NOW = datetime(2026, 11, 11, 12, 0)


def facts(**kw) -> StallFacts:
    base = {"status": "blocked", "live_lease": False, "owner_blocked": False,
            "recovery_hold": False, "attested": False, "waiting_until": None,
            "last_activity": NOW - STALL_AFTER - timedelta(hours=1), "snooze_until": None}
    return StallFacts(**{**base, **kw})


def test_quiet_task_without_any_holder_is_stalled():
    assert is_stalled(facts(), NOW)
    assert is_stalled(facts(status="open"), NOW)


@pytest.mark.parametrize("kw", [
    {"live_lease": True}, {"owner_blocked": True}, {"recovery_hold": True},
    {"attested": True}, {"status": "done"}, {"status": "cancelled"},
    {"status": "in_progress"}, {"last_activity": NOW - STALL_AFTER + timedelta(minutes=1)},
    {"waiting_until": NOW + timedelta(hours=1)}, {"snooze_until": NOW + timedelta(hours=1)},
])
def test_something_holds_the_task_so_it_is_not_stalled(kw):
    assert not is_stalled(facts(**kw), NOW)


def test_expired_wait_and_snooze_no_longer_hold():
    assert is_stalled(facts(waiting_until=NOW - timedelta(minutes=1),
                            snooze_until=NOW - timedelta(minutes=1)), NOW)


def test_plain_question_text_names_the_task_age_and_reason():
    text = question_text(title="Обновить stage", task_id="T1", project="Lamas", status="blocked",
                         note="Ждёт   DevOps\nпо stage", next_action=None, idle=timedelta(days=12))
    assert text.splitlines()[0] == "Стоит 12 дней: Обновить stage"
    assert "Проект: Lamas. Сейчас: заблокирована." in text
    assert "Последняя заметка: Ждёт DevOps по stage" in text
    assert "Следующий шаг" not in text and text.endswith("Что делаем?")


@pytest.mark.parametrize(("n", "word"), [(1, "день"), (2, "дня"), (5, "дней"), (11, "дней"),
                                         (21, "день"), (22, "дня"), (112, "дней")])
def test_days_word(n, word):
    assert days_word(n) == word


async def make(task_id: str, *, status: str = "blocked", age: timedelta = timedelta(days=5),
               creator: str = "dot") -> None:
    await tasks.open_task(room=R, task_id=task_id, agent=creator, title=f"Задача {task_id}",
                          paths=None, project="Vera")
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (R, task_id))
        row.status = status
        row.updated_at = row.created_at = utc_naive_now() - age


async def ids() -> list[str]:
    return [t.task_id for t in await stalled_tasks()]


@pytest.mark.asyncio
async def test_stalled_tasks_oldest_first_and_fresh_ones_skipped(sqlite_db):
    await make("OLD", age=timedelta(days=20))
    await make("MID", status="open", age=timedelta(days=4))
    await make("NEW", age=timedelta(hours=3))
    await make("FIN", status="done", age=timedelta(days=30))
    assert await ids() == ["OLD", "MID"]


@pytest.mark.asyncio
async def test_owner_pause_question_lease_and_snooze_take_task_off_the_list(sqlite_db):
    for t in ("P", "Q", "L", "S"):
        await make(t)
    await owner_actions.pause_by_owner(room=R, task_id="P", reason="стоп")
    async with get_session() as s:
        s.add(RoomTaskQuestionRow(room=R, task_id="Q", question="как?", asked_by="claude",
                                  status="open"))
        row = await s.get(RoomTaskRow, (R, "L"))
        row.lease_holder, row.lease_until = "claude", utc_naive_now() + timedelta(minutes=10)
    await owner_task_actions.snooze_by_owner(room=R, task_id="S", reason="позже")
    assert await ids() == []


@pytest.mark.asyncio
async def test_owner_cancels_a_task_created_by_another_agent(sqlite_db):
    await make("C", creator="dot")
    res = await owner_task_actions.cancel_by_owner(room=R, task_id="C", reason="не нужно",
                                                   via="telegram")
    assert res["changed"] is True and res["status"] == "cancelled"
    ev = (await task_progress.history(room=R, task_id="C", since_id=None, limit=50))[-1]
    assert ev["kind"] == "cancelled" and ev["agent"] == "owner"
    assert ev["data"]["by"] == "owner" and ev["data"]["via"] == "telegram"
    assert ev["data"]["prior_status"] == "blocked"
    again = await owner_task_actions.cancel_by_owner(room=R, task_id="C", reason="ещё раз")
    assert again["changed"] is False
    assert await ids() == []


@pytest.mark.asyncio
async def test_owner_cancel_beats_a_live_lease_and_the_holder_is_fenced_out(sqlite_db):
    await tasks.open_task(room=R, task_id="H", agent="dot", title="Т", paths=None)
    tok = (await tasks.claim(room=R, task_id="H", agent="claude", lease_seconds=600))
    await owner_task_actions.cancel_by_owner(room=R, task_id="H", reason="стоп")
    with pytest.raises(tasks.StaleLease):
        await tasks.update(room=R, task_id="H", agent="claude",
                           fencing_token=tok["fencing_token"], status="in_progress")


@pytest.mark.asyncio
async def test_owner_cancel_withdraws_open_questions_and_refuses_done(sqlite_db):
    await tasks.open_task(room=R, task_id="Q", agent="dot", title="Т", paths=None)
    tok = (await tasks.claim(room=R, task_id="Q", agent="claude", lease_seconds=600))
    await questions.ask(room=R, task_id="Q", agent="claude", fencing_token=tok["fencing_token"],
                        question="какой вариант?")
    await owner_task_actions.cancel_by_owner(room=R, task_id="Q", reason="отбой")
    async with get_session() as s:
        q = (await s.execute(RoomTaskQuestionRow.__table__.select())).first()
    assert q.status == "withdrawn"
    await make("D", status="done")
    with pytest.raises(TaskBusy):
        await owner_task_actions.cancel_by_owner(room=R, task_id="D", reason="x")


@pytest.mark.asyncio
async def test_continue_reopens_for_pickup_and_lifts_owner_pause(sqlite_db):
    await make("K")
    await owner_actions.pause_by_owner(room=R, task_id="K", reason="стоп")
    res = await owner_task_actions.continue_by_owner(room=R, task_id="K", reason="поехали")
    assert res["changed"] is True and res["status"] == "open" and res["auto_pickup"] is True
    kinds = [e["kind"] for e in await task_progress.history(room=R, task_id="K", since_id=None,
                                                             limit=50)]
    assert kinds[-2:] == ["resumed", "unblocked"]
    again = await owner_task_actions.continue_by_owner(room=R, task_id="K", reason="поехали")
    assert again["changed"] is False


@pytest.mark.asyncio
async def test_continue_does_not_steal_a_live_lease(sqlite_db):
    await tasks.open_task(room=R, task_id="B", agent="dot", title="Т", paths=None)
    await tasks.claim(room=R, task_id="B", agent="claude", lease_seconds=600)
    with pytest.raises(TaskBusy):
        await owner_task_actions.continue_by_owner(room=R, task_id="B", reason="x")


@pytest.mark.asyncio
async def test_snooze_hides_until_it_ends_and_keeps_updated_at(sqlite_db):
    await make("Z")
    async with get_session() as s:
        before = (await s.get(RoomTaskRow, (R, "Z"))).updated_at
    res = await owner_task_actions.snooze_by_owner(room=R, task_id="Z", reason="позже",
                                                   span=timedelta(seconds=-1))
    assert res["changed"] is True
    async with get_session() as s:
        assert (await s.get(RoomTaskRow, (R, "Z"))).updated_at == before
    assert await ids() == ["Z"]  # отсрочка уже истекла — задача снова на пульте


@pytest.mark.asyncio
async def test_actions_need_a_reason_and_an_existing_task(sqlite_db):
    await make("A")
    with pytest.raises(ValueError):
        await owner_task_actions.continue_by_owner(room=R, task_id="A", reason=" ")
    with pytest.raises(tasks.TaskNotFound):
        await owner_task_actions.snooze_by_owner(room=R, task_id="nope", reason="x")


@pytest.mark.parametrize(("task_id", "refs", "tracked"), [
    ("LAM-307", [], True),
    ("ORCH-620", None, True),
    ("orch-research-daily", [{"kind": "jira", "ref": "ORCH-1"}], True),
    ("x", [{"kind": "url", "ref": "https://itstep.atlassian.net/browse/SIN-1"}], True),
    ("x", [{"kind": "url", "ref": "https://jira.example/browse/LAM-5"}], True),
    ("help-vc-1", [{"kind": "url", "ref": "https://github.com/a/b/pull/3"}], False),
    ("webworms-wa-redraw", ["not-a-dict"], False),
])
def test_jira_tracked(task_id, refs, tracked):
    from vera_shared.room.stall_scope import jira_tracked
    assert jira_tracked(task_id, refs) is tracked


def test_excluded_projects_default_env_and_empty():
    from vera_shared.room.stall_scope import excluded_projects
    assert excluded_projects({}) == frozenset({"lamas", "sintegrum", "sinhrm", "ии-звонилка"})
    assert excluded_projects({"STALL_EXCLUDE_PROJECTS": " Foo, bar ,"}) == {"foo", "bar"}
    assert excluded_projects({"STALL_EXCLUDE_PROJECTS": ""}) == frozenset()


@pytest.mark.asyncio
async def test_jira_tasks_and_excluded_projects_stay_off_the_board(sqlite_db, monkeypatch):
    monkeypatch.delenv("STALL_EXCLUDE_PROJECTS", raising=False)
    await make("MINE")
    await make("LAM-1")
    await tasks.open_task(room=R, task_id="SINT", agent="dot", title="t", paths=None,
                          project="sintegrum")
    await tasks.open_task(room=R, task_id="REF", agent="dot", title="t", paths=None,
                          project="Orchestra", refs=[{"kind": "jira", "ref": "ORCH-9"}])
    async with get_session() as s:
        for t in ("SINT", "REF"):
            row = await s.get(RoomTaskRow, (R, t))
            row.status = "blocked"
            row.updated_at = row.created_at = utc_naive_now() - timedelta(days=5)
    assert await ids() == ["MINE"]

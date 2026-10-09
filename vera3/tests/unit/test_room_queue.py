"""Трекер задач, шаг 2b: attention в списках, очередь, ожидание, room_task_next."""
from __future__ import annotations

from datetime import datetime

import pytest
from tests.unit.test_mcp_room_tools import CLAUDE, CODEX, ctx
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room import tasks
from vera_shared.room.tasks import StaleLease

pytestmark = pytest.mark.asyncio

LONG_AGO = datetime(2020, 1, 1)
OWNER = ctx("owner")


async def set_row(task_id: str, **fields) -> None:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", task_id))
        for k, v in fields.items():
            setattr(row, k, v)


async def by_id(**kw) -> dict:
    return {t["task_id"]: t for t in (await r.room_tasks(**kw))["tasks"]}


async def test_room_tasks_carries_attention_and_queue_filters(sqlite_db):
    await r.room_task_open("free", CODEX)
    await r.room_task_claim("held", CLAUDE)
    await r.room_task_claim("gone", CLAUDE)
    await set_row("gone", lease_until=LONG_AGO)
    await r.room_task_claim("fin", CLAUDE)
    await r.room_task_release("fin", 1, CLAUDE)
    states = {k: v["attention"]["state"] for k, v in (await by_id()).items()}
    assert states == {"free": "unassigned", "held": "in_progress", "gone": "lease_expired",
                      "fin": "done"}
    assert set(await by_id(queue="open")) == {"free", "held", "gone"}
    assert set(await by_id(queue="unclaimed")) == {"free", "gone"}


async def test_open_question_marks_needs_owner_and_pause_comes_from_events(sqlite_db):
    await r.room_task_claim("T", CLAUDE)
    async with get_session() as s:
        s.add(RoomTaskQuestionRow(room="main", task_id="T", asked_by="claude", question="?"))
    assert (await by_id())["T"]["attention"]["state"] == "needs_owner"
    async with get_session() as s:
        (await s.get(RoomTaskQuestionRow, 1)).status = "answered"
    await r.room_task_state("T", 1, CLAUDE, "paused", "later")
    assert (await by_id())["T"]["attention"]["state"] == "paused"
    await r.room_task_state("T", 1, CLAUDE, "resumed", "go")
    assert (await by_id())["T"]["attention"]["state"] == "in_progress"


async def test_wait_sets_deadline_event_and_progress_clears_it(sqlite_db):
    await r.room_task_claim("T", CLAUDE)
    task_ = (await r.room_task_wait("T", 1, CLAUDE, 3600, "CI is running"))["task"]
    assert task_["waiting_reason"] == "CI is running" and task_["waiting_until"]
    assert (await by_id())["T"]["attention"]["state"] == "waiting"
    assert [e["kind"] for e in (await r.room_task_history("T"))["events"]][-1] == "waiting"
    await set_row("T", waiting_until=LONG_AGO)
    assert (await by_id())["T"]["attention"]["state"] == "stale_progress"
    cleared = (await r.room_task_progress("T", 1, CLAUDE, "CI green"))["task"]
    assert cleared["waiting_until"] is None


async def test_wait_requires_live_lease(sqlite_db):
    await r.room_task_claim("T", CLAUDE)
    with pytest.raises(StaleLease):
        await r.room_task_wait("T", 1, CODEX, 600, "x")


async def test_inbox_lists_my_tasks_with_attention(sqlite_db):
    await r.room_task_claim("mine", CLAUDE)
    await r.room_task_claim("theirs", CODEX)
    mine = (await r.room_inbox(CLAUDE))["my_tasks"]
    assert [(t["task_id"], t["attention"]["state"]) for t in mine] == [("mine", "in_progress")]


async def test_update_validates_and_stores_queue_fields(sqlite_db):
    await r.room_task_claim("T", CLAUDE)
    task_ = (await r.room_task_update("T", 1, CLAUDE, project="vera3", depends_on=["A"],
                                      auto_pickup=True))["task"]
    assert (task_["project"], task_["depends_on"], task_["auto_pickup"]) == (
        "vera3", ["A"], True)
    for bad in ({"depends_on": ["T"]}, {"depends_on": ["A", "A"]}, {"project": "bad name"}):
        with pytest.raises(ValueError, match="depend|project"):
            await tasks.update(room="main", task_id="T", agent="claude", fencing_token=1,
                               **bad)


# ─── room_task_next ──────────────────────────────────────────────────────────


async def token(task_id: str) -> int:
    return (await by_id())[task_id]["fencing_token"]


async def queued(task_id: str, *, auto_pickup: bool = True, **fields) -> None:
    await r.room_task_claim(task_id, OWNER)
    await r.room_task_update(task_id, await token(task_id), OWNER, auto_pickup=auto_pickup,
                             **fields)
    await r.room_task_release(task_id, await token(task_id), OWNER, status="open")


async def test_next_picks_by_priority_then_age_and_claims_with_fencing(sqlite_db):
    await queued("low", priority=3)
    await queued("urgent", priority=0)
    await queued("normal-a")
    await queued("normal-b")
    got = (await r.room_task_next(CLAUDE, session="s1", account="a1"))["task"]
    assert got["task_id"] == "urgent" and got["lease_holder"] == "claude"
    assert (got["fencing_token"], got["holder_session"]) == (2, "s1")
    order = [(await r.room_task_next(CLAUDE))["task"]["task_id"] for _ in range(3)]
    assert order == ["normal-a", "normal-b", "low"]
    assert (await r.room_task_next(CLAUDE))["task"] is None
    assert [e["kind"] for e in (await r.room_task_history("urgent"))["events"]][-1] == "claimed"


async def test_next_skips_manual_deps_pause_question_and_other_project(sqlite_db):
    await queued("manual", auto_pickup=False)
    await queued("needs-dep", depends_on=["dep"])
    await queued("missing-dep", depends_on=["ghost"])
    await queued("on-hold")
    await queued("asked")
    await queued("other", project="elsewhere")
    await r.room_task_claim("on-hold", OWNER)
    await r.room_task_state("on-hold", await token("on-hold"), OWNER, "paused", "hold")
    await r.room_task_release("on-hold", await token("on-hold"), OWNER, status="open")
    async with get_session() as s:
        s.add(RoomTaskQuestionRow(room="main", task_id="asked", asked_by="owner", question="?"))
    await r.room_task_claim("dep", CODEX)
    assert (await r.room_task_next(CLAUDE, project="vera3"))["task"] is None
    assert (await r.room_task_next(CLAUDE, project="elsewhere"))["task"]["task_id"] == "other"
    assert (await r.room_task_next(CLAUDE))["task"] is None
    await r.room_task_release("dep", 1, CODEX)
    assert (await r.room_task_next(CLAUDE))["task"]["task_id"] == "needs-dep"
    assert (await r.room_task_next(CLAUDE))["task"] is None


async def test_next_ignores_live_lease_and_takes_expired_open_task(sqlite_db):
    await queued("held")
    await r.room_task_claim("held", CODEX)
    assert (await r.room_task_next(CLAUDE))["task"] is None
    await set_row("held", status="open", lease_until=LONG_AGO)
    assert (await r.room_task_next(CLAUDE))["task"]["task_id"] == "held"

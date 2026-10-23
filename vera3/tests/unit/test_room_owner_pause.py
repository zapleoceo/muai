"""Пауза владельца (PR-B0b, дизайн §2.7 S5): ST5-ST7 и свойства pause_by_owner/resume_by_owner."""
from __future__ import annotations

from datetime import timedelta

import pytest
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room import handoff, owner_actions, questions, task_progress, tasks
from vera_shared.room.attention import PAUSED
from vera_shared.room.owner_block import BY_HOLDER, BY_OWNER, OwnerBlock, pause_state
from vera_shared.room.task_attention import attention_map
from vera_shared.room.task_queue import next_task
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
R, T = "main", "P1"


async def claim(agent: str = "claude") -> dict:
    return await tasks.claim(room=R, task_id=T, agent=agent, lease_seconds=600)


async def pauses() -> dict[str, bool]:
    async with get_session() as s:
        return await pause_state(s, R, T)


async def history() -> list[dict]:
    return await task_progress.history(room=R, task_id=T, since_id=None, limit=200)


async def kinds() -> list[str]:
    return [e["kind"] for e in await history()]


async def set_state(tok: int, state: str) -> dict:
    return await task_progress.set_state(room=R, task_id=T, agent="claude",
                                         fencing_token=tok, state=state, reason="x")


async def paused_attention() -> bool:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (R, T))
        return (await attention_map(s, [row], utc_naive_now()))[(R, T)].state == PAUSED


async def offer_and_accept(tok: int) -> dict:
    await handoff.offer(room=R, task_id=T, agent="claude", fencing_token=tok, to_agent="codex")
    return await handoff.accept(room=R, task_id=T, agent="codex")


async def test_owner_pause_needs_no_lease_and_records_actor_and_reason(sqlite_db):
    await claim()
    async with get_session() as s:
        (await s.get(RoomTaskRow, (R, T))).lease_until = utc_naive_now()  # аренда истекла
    res = await owner_actions.pause_by_owner(room=R, task_id=T, reason="стоп до пятницы")
    assert res["changed"] is True and res["status"] == "in_progress"
    ev = [e for e in await history() if e["kind"] == "paused"][0]
    assert ev["agent"] == "owner" and ev["text"] == "стоп до пятницы"
    assert ev["data"] == {"by": BY_OWNER, "actor": "owner", "reason": "стоп до пятницы",
                          "via": "dashboard"}
    assert await pauses() == {BY_HOLDER: False, BY_OWNER: True}


async def test_reason_is_required_and_bounded(sqlite_db):
    await claim()
    for bad in ("", "   ", "x" * 2001):
        with pytest.raises(ValueError):
            await owner_actions.pause_by_owner(room=R, task_id=T, reason=bad)
        with pytest.raises(ValueError):
            await owner_actions.resume_by_owner(room=R, task_id=T, reason=bad)
    assert "paused" not in await kinds()


async def test_unknown_and_final_tasks_are_refused(sqlite_db):
    with pytest.raises(tasks.TaskNotFound):
        await owner_actions.pause_by_owner(room=R, task_id="nope", reason="r")
    tok = (await claim())["fencing_token"]
    await tasks.release(room=R, task_id=T, agent="claude", fencing_token=tok, status="done")
    with pytest.raises(tasks.TaskBusy):
        await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")


async def test_repeat_pause_and_resume_write_nothing(sqlite_db):
    await claim()
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    assert (await owner_actions.pause_by_owner(room=R, task_id=T, reason="r2"))["changed"] is False
    await owner_actions.resume_by_owner(room=R, task_id=T, reason="r")
    assert (await owner_actions.resume_by_owner(room=R, task_id=T, reason="r"))["changed"] is False
    got = await kinds()
    assert got.count("paused") == 1 and got.count("resumed") == 1


async def test_st5_holder_resume_cannot_lift_owner_pause_and_gates_hold(sqlite_db):
    tok = (await claim())["fencing_token"]
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    with pytest.raises(OwnerBlock):
        await set_state(tok, "resumed")
    assert (await pauses())[BY_OWNER] is True and await paused_attention()
    with pytest.raises(handoff.HandoffError):
        await offer_and_accept(tok)


async def test_st5_next_skips_owner_paused_task_until_owner_resumes(sqlite_db):
    await tasks.open_task(room=R, task_id=T, agent="claude", title="t", paths=None,
                          auto_pickup=True)
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    pick = {"room": R, "agent": "codex", "project": None, "lease_seconds": 60,
            "session": None, "account": None}
    assert await next_task(**pick) is None
    await owner_actions.resume_by_owner(room=R, task_id=T, reason="r")
    assert (await next_task(**pick))["task_id"] == T


async def test_st6_holder_pause_and_resume_leave_owner_pause_untouched(sqlite_db):
    tok = (await claim())["fencing_token"]
    await set_state(tok, "paused")
    assert await pauses() == {BY_HOLDER: True, BY_OWNER: False}
    await set_state(tok, "resumed")
    assert await pauses() == {BY_HOLDER: False, BY_OWNER: False}
    await set_state(tok, "paused")
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    assert await pauses() == {BY_HOLDER: True, BY_OWNER: True}


async def test_st7_owner_resume_lifts_only_owner_pause(sqlite_db):
    tok = (await claim())["fencing_token"]
    await set_state(tok, "paused")
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    await owner_actions.resume_by_owner(room=R, task_id=T, reason="r")
    assert await pauses() == {BY_HOLDER: True, BY_OWNER: False}
    assert await paused_attention()  # пауза держателя в воротах и внимании осталась
    with pytest.raises(handoff.HandoffError):
        await offer_and_accept(tok)
    await set_state(tok, "resumed")
    assert await pauses() == {BY_HOLDER: False, BY_OWNER: False}
    assert not await paused_attention()


async def test_ack_keeps_blocked_under_real_owner_pause_then_holder_unblocks_after_resume(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = (await questions.ask(room=R, task_id=T, agent="claude", fencing_token=tok,
                               question="?"))["qid"]
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    await questions.ack(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok)
    async with get_session() as s:
        assert (await s.get(RoomTaskRow, (R, T))).status == "blocked"
    assert "unblocked" not in await kinds()
    await owner_actions.resume_by_owner(room=R, task_id=T, reason="r")
    assert (await set_state(tok, "unblocked"))["status"] == "in_progress"


async def snapshot() -> tuple:
    async with get_session() as s:
        r = await s.get(RoomTaskRow, (R, T))
        return (r.status, r.fencing_token, r.lease_holder, r.lease_until)


async def test_claim_of_open_owner_paused_task_is_refused_and_changes_nothing(sqlite_db):
    await tasks.open_task(room=R, task_id=T, agent="claude", title="t", paths=None)
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    before, events = await snapshot(), await kinds()
    with pytest.raises(tasks.TaskBusy, match="paused by the owner"):
        await claim("codex")
    assert await snapshot() == before and await kinds() == events
    assert before[0] == "open" and before[2] is None


async def test_claim_after_expiry_is_refused_even_for_the_same_agent(sqlite_db):
    await claim()
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    async with get_session() as s:
        (await s.get(RoomTaskRow, (R, T))).lease_until = utc_naive_now() - timedelta(seconds=5)
    before, events = await snapshot(), await kinds()
    for agent in ("claude", "codex"):
        with pytest.raises(tasks.TaskBusy, match="paused by the owner"):
            await claim(agent)
    assert await snapshot() == before and await kinds() == events


async def test_claim_is_allowed_again_after_owner_resume(sqlite_db):
    await tasks.open_task(room=R, task_id=T, agent="claude", title="t", paths=None)
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    await owner_actions.resume_by_owner(room=R, task_id=T, reason="r")
    got = await claim("codex")
    assert got["status"] == "in_progress" and got["fencing_token"] == 1


async def test_live_holder_extension_during_owner_pause_keeps_fencing_and_status(sqlite_db):
    first = await claim()
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    again = await claim()
    assert again["fencing_token"] == first["fencing_token"]
    assert again["status"] == first["status"] == "in_progress"
    assert again["lease_until"] >= first["lease_until"]
    assert (await kinds())[-1] == "heartbeat"
    with pytest.raises(tasks.TaskBusy):  # чужой живой держатель по-прежнему получает отказ
        await claim("codex")

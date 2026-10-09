"""Трекер задач, шаг 4: передача задачи с подтверждением получателя."""
from __future__ import annotations

import pytest
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room import handoff, messages, task_progress, tasks
from vera_shared.room.attention import HANDOFF_PENDING, attention
from vera_shared.room.tasks import StaleLease
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio


async def held(task_id: str = "H1") -> dict:
    return await tasks.claim(room="main", task_id=task_id, agent="claude", lease_seconds=600)


async def test_offer_keeps_lease_with_sender_and_notifies_receiver(sqlite_db):
    await held()
    t = await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=1,
                            to_agent="codex", note="допиши тесты")
    assert (t["lease_holder"], t["pending_handoff_to"], t["fencing_token"]) == (
        "claude", "codex", 1)
    items, _ = await messages.inbox(agent="codex", room="main", consumer="default",
                                    limit=10, since_id=None)
    assert [m["task_id"] for m in items] == ["H1"] and "допиши тесты" in items[0]["body"]
    await tasks.update(room="main", task_id="H1", agent="claude", fencing_token=1,
                       extend_seconds=600)


async def test_accept_moves_lease_bumps_token_and_old_token_is_stale(sqlite_db):
    await held()
    await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=1,
                        to_agent="codex")
    t = await handoff.accept(room="main", task_id="H1", agent="codex", session="s9",
                             account="acc")
    assert (t["lease_holder"], t["fencing_token"], t["pending_handoff_to"]) == (
        "codex", 2, None)
    assert (t["holder_session"], t["holder_account"]) == ("s9", "acc")
    with pytest.raises(StaleLease):
        await tasks.update(room="main", task_id="H1", agent="claude", fencing_token=1,
                           note="я всё ещё тут")
    await tasks.update(room="main", task_id="H1", agent="codex", fencing_token=2, note="ok")
    kinds = [e["kind"] for e in await task_progress.history(
        room="main", task_id="H1", since_id=None, limit=50)]
    assert "handoff_offer" in kinds and "handoff_accept" in kinds


async def test_only_the_receiver_may_accept_and_offer_needs_a_live_lease(sqlite_db):
    await held()
    with pytest.raises(handoff.HandoffError):
        await handoff.accept(room="main", task_id="H1", agent="codex")  # предложения нет
    await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=1,
                        to_agent="codex")
    with pytest.raises(handoff.HandoffError):
        await handoff.accept(room="main", task_id="H1", agent="gemini")
    with pytest.raises(handoff.HandoffError):
        await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=1,
                            to_agent="gemini")
    with pytest.raises(StaleLease):
        await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=7,
                            to_agent="codex")
    with pytest.raises(ValueError, match="yourself"):
        await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=1,
                            to_agent="claude")


async def test_decline_and_cancel_clear_pending_and_keep_the_sender(sqlite_db):
    await held()
    await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=1,
                        to_agent="codex")
    with pytest.raises(handoff.HandoffError):
        await handoff.decline(room="main", task_id="H1", agent="gemini")
    t = await handoff.decline(room="main", task_id="H1", agent="codex", reason="занят")
    assert (t["pending_handoff_to"], t["lease_holder"], t["fencing_token"]) == (
        None, "claude", 1)
    await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=1,
                        to_agent="codex")
    with pytest.raises(StaleLease):
        await handoff.cancel(room="main", task_id="H1", agent="codex", fencing_token=1)
    t = await handoff.cancel(room="main", task_id="H1", agent="claude", fencing_token=1)
    assert t["pending_handoff_to"] is None
    evs = await task_progress.history(room="main", task_id="H1", since_id=None, limit=50)
    actions = [e["data"]["action"] for e in evs if e["kind"] == "handoff_offer"]
    assert actions == ["offer", "decline", "offer", "cancel"]


async def test_attention_shows_pending_handoff_and_release_clears_it(sqlite_db):
    await held()
    await handoff.offer(room="main", task_id="H1", agent="claude", fencing_token=1,
                        to_agent="codex")
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", "H1"))
        att = attention(row, utc_naive_now())
    assert (att.state, att.label_ru) == (HANDOFF_PENDING, "ждёт приёма codex")
    t = await tasks.release(room="main", task_id="H1", agent="claude", fencing_token=1,
                            status="open")
    assert t["pending_handoff_to"] is None


async def test_accept_rejected_for_done_cancelled_and_paused(sqlite_db):
    for tid, how in (("X1", "done"), ("X2", "cancelled"), ("X3", "paused")):
        await held(tid)
        await handoff.offer(room="main", task_id=tid, agent="claude", fencing_token=1,
                            to_agent="codex")
        if how == "paused":
            await task_progress.set_state(room="main", task_id=tid, agent="claude",
                                          fencing_token=1, state="paused", reason="r")
        else:
            async with get_session() as s:
                (await s.get(RoomTaskRow, ("main", tid))).status = how
        with pytest.raises(handoff.HandoffError, match=how):
            await handoff.accept(room="main", task_id=tid, agent="codex")

"""Тихий режим владельца: переходы active/wait10/quiet, переживание рестарта, сводка."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskQuestionRow, RoomTaskRow
from vera_shared.owner_attention import gate, store
from vera_shared.owner_attention.policy import timeout_goes_quiet

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("sqlite_db")]

VN = timedelta(hours=7)


def vn(h: int, m: int, day: int = 11) -> datetime:
    """Местное время Asia/Ho_Chi_Minh -> наивный UTC, как в БД."""
    return datetime(2026, 10, day, h, m) - VN


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, _html: str, plain: str) -> int:
        self.sent.append(plain)
        return len(self.sent)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("OWNER_QUIET_ENABLED", "1")
    monkeypatch.delenv("OWNER_QUIET_COUNT_CROSS_MIDNIGHT", raising=False)


async def _ask(send: Recorder, now: datetime, key: str = "q1", **kw) -> int | None:
    return await gate.deliver(send, key, key, kind=gate.QUESTION, key=key, now=now, **kw)


async def _go_quiet(send: Recorder) -> None:
    await _ask(send, vn(0, 10))
    await gate.tick(send, vn(0, 21))


async def test_question_starts_wait10():
    send = Recorder()
    await _ask(send, vn(0, 10))
    st = await store.load_state()
    assert (st.state, st.wait_deadline) == (store.WAIT10, vn(0, 20))
    assert send.sent == ["q1"]


async def test_status_and_alert_keep_active():
    send = Recorder()
    await gate.deliver(send, "s", "s", kind="status", key="s", now=vn(0, 10))
    await gate.deliver(send, "a", "a", kind="alert", key="a", now=vn(0, 11))
    assert (await store.load_state()).state == store.ACTIVE
    assert send.sent == ["s", "a"]


async def test_answer_before_deadline_returns_active():
    await _ask(Recorder(), vn(0, 10))
    await gate.on_owner_message(vn(0, 15))
    assert (await store.load_state()).state == store.ACTIVE
    await gate.tick(Recorder(), vn(0, 30))
    assert (await store.load_state()).state == store.ACTIVE


async def test_timeout_after_midnight_goes_quiet_for_8h():
    await _go_quiet(Recorder())
    st = await store.load_state()
    assert (st.state, st.quiet_until) == (store.QUIET, vn(8, 20))


async def test_timeout_before_midnight_stays_active():
    send = Recorder()
    await _ask(send, vn(22, 0, day=10))
    await gate.tick(send, vn(22, 11, day=10))
    assert (await store.load_state()).state == store.ACTIVE


async def test_cross_midnight_rule_is_off_by_default():
    deadline = vn(0, 5)
    assert timeout_goes_quiet(deadline) is False
    assert timeout_goes_quiet(deadline, cross_midnight=True) is True


async def test_late_answer_during_quiet_is_active_at_once():
    await _go_quiet(Recorder())
    await gate.on_owner_message(vn(3, 0))
    assert (await store.load_state()).state == store.ACTIVE
    send = Recorder()
    await gate.deliver(send, "x", "x", kind="status", key="x", now=vn(3, 1))
    assert send.sent == ["x"]


async def test_new_question_later_starts_new_wait10():
    send = Recorder()
    await _ask(send, vn(0, 10))
    await gate.on_owner_message(vn(0, 12))
    await _ask(send, vn(1, 0), key="q2")
    st = await store.load_state()
    assert (st.state, st.wait_question_ref, st.wait_deadline) == (store.WAIT10, "q2", vn(1, 10))


async def test_restart_resumes_wait10_and_quiet_from_db():
    await _ask(Recorder(), vn(0, 10))
    assert (await store.load_state()).state == store.WAIT10  # новый "процесс": память пуста
    await gate.tick(Recorder(), vn(0, 25))
    assert (await store.load_state()).state == store.QUIET
    send = Recorder()
    await gate.deliver(send, "late", "late", kind="alert", key="late", task_id="T9", now=vn(2, 0))
    assert send.sent == []
    assert [i.dedup_key for i in await store.pending()] == ["late"]


async def test_wait10_does_not_hold_notifications():
    send = Recorder()
    await _ask(send, vn(0, 10))
    await gate.deliver(send, "s", "s", kind="status", key="s", now=vn(0, 12))
    assert send.sent == ["q1", "s"]


async def test_flush_sends_one_digest_and_is_idempotent():
    await _go_quiet(Recorder())
    quiet = Recorder()
    for key, text, task in (("a1", "T1 стала", "T1"), ("a2", "T1 снова", "T1"), ("b", "T2 ждёт", "T2")):
        await gate.deliver(quiet, text, text, kind="alert", key=key, task_id=task, now=vn(4, 0))
    assert quiet.sent == []
    send = Recorder()
    await gate.tick(send, vn(8, 20))
    await gate.tick(send, vn(8, 21))
    assert len(send.sent) == 1
    assert "T1 снова" in send.sent[0] and "T1 стала" not in send.sent[0]
    assert "T2 ждёт" in send.sent[0]
    assert (await store.load_state()).state == store.ACTIVE
    assert await store.pending() == []


async def test_digest_drops_answered_questions_and_done_tasks():
    async with get_session() as s:
        s.add(RoomTaskRow(room="r", task_id="DONE", created_by="x", status="done"))
        q = RoomTaskQuestionRow(room="r", task_id="OPEN", asked_by="x", question="?", status="answered")
        s.add(q)
        await s.commit()
        qid = q.qid
    await _go_quiet(Recorder())
    quiet = Recorder()
    await gate.deliver(quiet, "done-alert", "done-alert", kind="alert", key="d",
                       task_id="DONE", room="r", now=vn(4, 0))
    await gate.deliver(quiet, "answered-q", "answered-q", kind=gate.QUESTION, key="q",
                       task_id="OPEN", room="r", question_id=qid, now=vn(4, 1))
    await gate.deliver(quiet, "live", "live", kind="alert", key="l", task_id="LIVE", now=vn(4, 2))
    send = Recorder()
    await gate.tick(send, vn(8, 20))
    assert len(send.sent) == 1
    assert "live" in send.sent[0]
    assert "done-alert" not in send.sent[0] and "answered-q" not in send.sent[0]


async def test_flag_off_sends_immediately_and_keeps_state(monkeypatch):
    monkeypatch.setenv("OWNER_QUIET_ENABLED", "0")
    send = Recorder()
    await _ask(send, vn(0, 10))
    await gate.tick(send, vn(3, 0))
    await gate.deliver(send, "n", "n", kind="alert", key="n", now=vn(3, 1))
    assert send.sent == ["q1", "n"]
    assert (await store.load_state()).state == store.ACTIVE
    assert await store.pending() == []

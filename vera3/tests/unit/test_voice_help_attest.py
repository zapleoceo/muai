"""Делегированное подтверждение dot: границы, идемпотентность, owner-путь не тронут."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import dispatcher, voice_worker
from gateway.voice_command import accept_voice_command
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskAnswerRow, RoomTaskEventRow, RoomTaskRow
from vera_shared.office.repository import revision_of
from vera_shared.room import questions, tasks
from vera_shared.room.task_queue import next_task
from vera_shared.voice_help import attest as svc
from vera_shared.voice_help.hold import lift_hold
from vera_shared.voice_help.policy import (
    ATTEST_SCOPE_REF,
    ATTESTED_ACTION,
    SOURCE_ATTESTED,
    SOURCE_CONFIRMED,
    attest_recipient_blocked,
    attest_unsafe_reason,
)
from voice_help_kit import OWNER, ROOM, SECRET, Recorder, answer, help_cmd, tasks_in

TID = "help-vc-a"
SAFE = "ответь мне ок"
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _env(monkeypatch, sqlite_db):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)
    monkeypatch.setenv("ROOM_ATTEST_ENABLED", "1")
    monkeypatch.delenv("ROOM_ATTEST_ACTORS", raising=False)


async def _open(instruction: str = SAFE, cid: str = "vc-a") -> int:
    await accept_voice_command(help_cmd(cid, instruction=instruction, doubts=["blind"]),
                               x_internal_secret=SECRET)
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        assert await voice_worker.process_one(Recorder(), OWNER)
    [q] = await questions.questions_of(ROOM, f"help-{cid}")
    return q["qid"]


async def _rev(task_id: str = TID) -> int:
    async with get_session() as s:
        return await revision_of(s, ROOM, task_id)


async def _call(qid: int, **over):
    args = dict(principal="dot", room=ROOM, task_id=TID, qid=qid,
                expected_revision=await _rev(), evidence_message_id="m-1",
                evidence_at="2026-10-11T10:00:00+00:00", quoted_text="да, это я сказал",
                classification=svc.CLASSIFICATION, idempotency_key="k1")
    args.update(over)
    return await svc.attest(**args)


async def _rows():
    async with get_session() as s:
        ans = (await s.execute(select(RoomTaskAnswerRow))).scalars().all()
        evs = (await s.execute(select(RoomTaskEventRow).where(
            RoomTaskEventRow.kind == "delegated_attestation"))).scalars().all()
    return list(ans), list(evs)


async def test_non_dot_forbidden():
    qid = await _open()
    with pytest.raises(svc.AttestForbidden):
        await _call(qid, principal="codex")
    assert await _rows() == ([], [])


async def test_flag_off_disabled(monkeypatch):
    qid = await _open()
    monkeypatch.setenv("ROOM_ATTEST_ENABLED", "0")
    with pytest.raises(svc.AttestDisabled):
        await _call(qid)


async def test_wrong_classification_invalid():
    qid = await _open()
    with pytest.raises(svc.InvalidAttestation):
        await _call(qid, classification="forwarded")


async def test_revision_mismatch_conflict():
    qid = await _open()
    with pytest.raises(svc.AttestConflict):
        await _call(qid, expected_revision=(await _rev()) + 5)
    assert await _rows() == ([], [])


async def test_non_voice_task_invalid_scope():
    qid = await _open()
    await tasks.open_task(room=ROOM, task_id="plain", agent="x", title="t", paths=None)
    with pytest.raises(svc.InvalidScope):
        await _call(qid, task_id="plain", expected_revision=await _rev("plain"))


async def test_non_authorship_question_invalid_scope():
    await _open()
    claimed = await tasks.claim(room=ROOM, task_id=TID, agent="codex", lease_seconds=60)
    other = await questions.ask(room=ROOM, task_id=TID, agent="codex",
                                fencing_token=claimed["fencing_token"],
                                question="можно удалить?")
    with pytest.raises(svc.InvalidScope):
        await _call(other["qid"])


async def test_answered_question_invalid_scope():
    qid = await _open()
    await questions.answer(room=ROOM, task_id=TID, qid=qid, text="да")
    with pytest.raises(svc.InvalidScope):
        await _call(qid)


async def test_closed_task_invalid_scope():
    qid = await _open()
    claimed = await tasks.claim(room=ROOM, task_id=TID, agent="codex", lease_seconds=60)
    await tasks.release(room=ROOM, task_id=TID, agent="codex",
                        fencing_token=claimed["fencing_token"], status="done")
    with pytest.raises(svc.InvalidScope):
        await _call(qid)


async def test_happy_path_one_answer_event_refs_and_unblocks():
    qid = await _open()
    assert await next_task(room=ROOM, agent="codex", project=None, lease_seconds=60,
                           session=None, account=None) is None
    res = await _call(qid, quoted_text="да, это я, ключ sk-ant-api03-" + "a" * 40)
    assert res["replayed"] is False
    ans, evs = await _rows()
    assert [a.answered_by for a in ans] == ["dot-attested"]
    assert len(evs) == 1 and evs[0].data["attested_by"] == "dot"
    assert evs[0].data["evidence_message_id"] == "m-1"
    assert "sk-ant" not in evs[0].data["quoted_text"] and "sk-ant" not in ans[0].text
    [task] = await tasks_in(ROOM)
    refs = [r["ref"] for r in task.refs]
    assert SOURCE_ATTESTED in refs and ATTEST_SCOPE_REF in refs
    assert SOURCE_CONFIRMED not in refs
    assert task.next_action == ATTESTED_ACTION
    picked = await next_task(room=ROOM, agent="codex", project=None, lease_seconds=60,
                             session=None, account=None)
    assert picked is not None and picked["task_id"] == TID


async def test_repeat_replays_and_never_two_answers():
    qid = await _open()
    first = await _call(qid)
    rev = first["revision"]
    again = await _call(qid, expected_revision=rev)
    assert again["replayed"] is True and again["revision"] == rev
    with pytest.raises(svc.AttestConflict):
        await _call(qid, quoted_text="другое", expected_revision=rev)
    with pytest.raises(svc.AttestConflict):
        await _call(qid, idempotency_key="k2", expected_revision=rev)
    ans, evs = await _rows()
    assert len(ans) == 1 and len(evs) == 1


async def test_attested_answer_is_not_owner_and_owner_still_lifts():
    qid = await _open()
    await _call(qid)
    assert not await lift_hold(ROOM, TID, "обычное действие")
    await questions.answer(room=ROOM, task_id=TID, qid=qid, text="да, я")
    assert await lift_hold(ROOM, TID, "обычное действие")
    [task] = await tasks_in(ROOM)
    assert SOURCE_CONFIRMED in [r["ref"] for r in task.refs]


@pytest.mark.parametrize("text", [
    "отправь отчёт Пете", "перешли это", "напиши Пете привет", "скинь ссылку",
    "найди договор", "покажи баланс", "вспомни что говорили", "сколько стоит",
    "открой https://x.example", "запусти сборку", "удали файл", "задеплой бота",
    "оплати счёт", "переведи 100 $", "купи билеты", "ответь ок и отправь Пете",
    "x" * 201,
])
async def test_unsafe_instruction_invalid_scope(text):
    assert attest_unsafe_reason(text)
    qid = await _open(text)
    with pytest.raises(svc.InvalidScope):
        await _call(qid)
    assert await _rows() == ([], [])


def test_plain_reply_is_safe():
    assert attest_unsafe_reason(SAFE) is None


def test_recipient_enforcement_helper():
    refs = [{"kind": "source", "ref": SOURCE_ATTESTED},
            {"kind": "source", "ref": ATTEST_SCOPE_REF}]
    assert attest_recipient_blocked(refs, "dot") and attest_recipient_blocked(refs, "petya")
    assert not attest_recipient_blocked(refs, "owner")
    assert not attest_recipient_blocked(refs, None)
    assert not attest_recipient_blocked([], "dot")


async def test_dispatcher_blocks_attested_task_with_foreign_recipient():
    await _open()
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (ROOM, TID))
        row.refs = [*row.refs, {"kind": "source", "ref": SOURCE_ATTESTED},
                    {"kind": "source", "ref": ATTEST_SCOPE_REF},
                    {"kind": "source", "ref": "result_recipient: dot"}]
    posted = AsyncMock()
    claimed = await tasks.claim(room=ROOM, task_id=TID, agent="vera", lease_seconds=60)
    with patch.object(dispatcher, "post_message", posted):
        await dispatcher._attempt(claimed)
    posted.assert_not_called()
    [task] = await tasks_in(ROOM)
    assert task.status == "blocked"

"""Делегированное подтверждение dot: границы, идемпотентность, owner-путь не тронут."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import dispatcher, voice_worker
from gateway.voice_command import accept_voice_command
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskAnswerRow, RoomTaskEventRow, RoomTaskRow
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.office.repository import revision_of
from vera_shared.room import (
    handoff,
    messages,
    questions,
    task_progress,
    task_wait,
    tasks,
)
from vera_shared.room.task_queue import next_task
from vera_shared.voice_help import attest as svc
from vera_shared.voice_help.hold import lift_hold
from vera_shared.voice_help.policy import (
    ATTEST_SCOPE_REF,
    SOURCE_ATTESTED,
    SOURCE_CONFIRMED,
    attest_recipient_blocked,
    attest_reply_text,
    attested_action,
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
    args = {"principal": "dot", "room": ROOM, "task_id": TID, "qid": qid,
            "expected_revision": await _rev(), "evidence_message_id": "m-1",
            "evidence_at": "2026-10-11T10:00:00+00:00", "quoted_text": "да, это я сказал",
            "classification": svc.CLASSIFICATION, "idempotency_key": "k1"}
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


async def test_evidence_timestamp_requires_timezone():
    qid = await _open()
    with pytest.raises(svc.InvalidAttestation):
        await _call(qid, evidence_at="2026-10-11T10:00:00")


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
    assert task.next_action == attested_action("ок")
    assert task.auto_pickup is False
    assert evs[0].data["allowed_reply"] == "ок"
    picked = await next_task(room=ROOM, agent="codex", project=None, lease_seconds=60,
                             session=None, account=None)
    assert picked is None
    with pytest.raises(tasks.TaskBusy):
        await tasks.claim(room=ROOM, task_id=TID, agent="codex", lease_seconds=60)
    explicit = await tasks.claim(room=ROOM, task_id=TID, agent="dot", lease_seconds=60)
    assert explicit["task_id"] == TID


async def test_source_event_survives_cleared_queue_instruction():
    qid = await _open("Ответь словом Привет")
    async with get_session() as s:
        command = await s.get(VoiceCommandRow, "vc-a")
        command.instruction = ""
    result = await _call(qid)
    assert result["ok"] is True
    [task] = await tasks_in(ROOM)
    assert task.next_action == attested_action("Привет")


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


async def test_attested_completion_requires_exact_receipt_and_is_idempotent():
    qid = await _open()
    attested = await _call(qid)
    lease = await tasks.claim(room=ROOM, task_id=TID, agent="dot", lease_seconds=60)
    token = lease["fencing_token"]
    with pytest.raises(tasks.TaskBusy):
        await tasks.update(room=ROOM, task_id=TID, agent="dot", fencing_token=token,
                           next_action="send elsewhere")
    with pytest.raises(tasks.TaskBusy):
        await tasks.release(room=ROOM, task_id=TID, agent="dot",
                            fencing_token=token, status="done", note="sent")
    with pytest.raises(handoff.HandoffError):
        await handoff.offer(room=ROOM, task_id=TID, agent="dot", fencing_token=token,
                            to_agent="vera")
    with pytest.raises(ValueError):
        await task_progress.progress(room=ROOM, task_id=TID, agent="dot",
                                     fencing_token=token, result="sent something")
    with pytest.raises(ValueError):
        await task_wait.wait(room=ROOM, task_id=TID, agent="dot", fencing_token=token,
                             until_seconds=60, reason="later")
    with pytest.raises(questions.QuestionState):
        await questions.ask(room=ROOM, task_id=TID, agent="dot",
                            fencing_token=token, question="something else")
    with pytest.raises(messages.MessageConflict):
        await messages.post_message(room=ROOM, message_id="arbitrary", from_agent="dot",
                                    body="other", to_agent="someone", task_id=TID)
    with pytest.raises(tasks.TaskBusy):
        await tasks.complete_attested(room=ROOM, task_id=TID, agent="dot",
                                     fencing_token=token, reply_text="wrong",
                                     receipt_id="receipt-1")
    word = attest_reply_text(SAFE)
    done = await tasks.complete_attested(room=ROOM, task_id=TID, agent="dot",
                                         fencing_token=token, reply_text=word,
                                         receipt_id="receipt-1")
    assert done["task"]["status"] == "done" and not done["replayed"]
    again = await tasks.complete_attested(room=ROOM, task_id=TID, agent="dot",
                                          fencing_token=token, reply_text=word,
                                          receipt_id="receipt-1")
    assert again["replayed"]
    with pytest.raises(tasks.TaskBusy):
        await tasks.complete_attested(room=ROOM, task_id=TID, agent="dot",
                                      fencing_token=token, reply_text=word,
                                      receipt_id="receipt-2")
    assert (await _call(qid, expected_revision=attested["revision"]))["replayed"]
    async with get_session() as s:
        done_events = (await s.execute(select(RoomTaskEventRow).where(
            RoomTaskEventRow.room == ROOM, RoomTaskEventRow.task_id == TID,
            RoomTaskEventRow.kind == "done"))).scalars().all()
    assert len(done_events) == 1
    assert done_events[0].data["receipt_id"] == "receipt-1"


async def test_receipt_cannot_complete_two_voice_tasks():
    first_qid = await _open()
    await _call(first_qid)
    first = await tasks.claim(room=ROOM, task_id=TID, agent="dot", lease_seconds=60)
    word = attest_reply_text(SAFE)
    await tasks.complete_attested(room=ROOM, task_id=TID, agent="dot",
                                  fencing_token=first["fencing_token"],
                                  reply_text=word, receipt_id="shared-receipt")
    second_qid = await _open(cid="vc-b")
    second_id = "help-vc-b"
    await _call(second_qid, task_id=second_id, expected_revision=await _rev(second_id),
                idempotency_key="k2")
    second = await tasks.claim(room=ROOM, task_id=second_id, agent="dot",
                               lease_seconds=60)
    with pytest.raises(tasks.TaskBusy, match="receipt is already bound"):
        await tasks.complete_attested(room=ROOM, task_id=second_id, agent="dot",
                                      fencing_token=second["fencing_token"],
                                      reply_text=word, receipt_id="shared-receipt")


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
    "ответь на вопрос по памяти", "ответь мне после проверки", "скажи Пете ок",
    "ответь словом Привет и удали файл", "ответь ссылкой", "ответь мне два слова",
    "Ответь словом «Привет и отправь Пете»",
    "Ответь словом «Привет», отправь Пете",
    "Ответь словом «отправь Пете»",
    "Ответь словом «ссылкой»",
    "Ответь словом «Привет", "Ответь словом Привет»",
    "Передай ДОТ тест офиса. Ответь числом 42",
    "x" * 201,
])
async def test_unsafe_instruction_invalid_scope(text):
    assert attest_reply_text(text) is None
    qid = await _open(text)
    with pytest.raises(svc.InvalidScope):
        await _call(qid)
    assert await _rows() == ([], [])


async def test_plain_reply_is_safe():
    assert attest_reply_text(SAFE) == "ок"
    assert attest_reply_text("Вера, мне нужна помощь. Ответь словом Привет") == "Привет"
    assert attest_reply_text("Ответь словом «Привет»") == "Привет"
    assert (attest_reply_text("Вера, мне нужна помощь. Ответь словом «Привет»")
            == "Привет")


async def test_quoted_single_word_attests_without_broadening_scope():
    qid = await _open("Ответь словом «Привет»")
    result = await _call(qid)
    assert result["replayed"] is False
    answers, events = await _rows()
    assert [a.answered_by for a in answers] == ["dot-attested"]
    assert len(events) == 1 and events[0].data["allowed_reply"] == "Привет"


async def test_recipient_enforcement_helper():
    refs = [{"kind": "source", "ref": SOURCE_ATTESTED},
            {"kind": "source", "ref": ATTEST_SCOPE_REF}]
    assert attest_recipient_blocked(refs, "dot") and attest_recipient_blocked(refs, "petya")
    assert not attest_recipient_blocked(refs, "owner")
    assert attest_recipient_blocked(refs, None)
    assert not attest_recipient_blocked([], "dot")


async def test_missing_recipient_blocks_dispatch():
    await _open()
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (ROOM, TID))
        row.refs = [*row.refs, {"kind": "source", "ref": SOURCE_ATTESTED},
                    {"kind": "source", "ref": ATTEST_SCOPE_REF}]
    posted = AsyncMock()
    claimed = await tasks.claim(room=ROOM, task_id=TID, agent="vera", lease_seconds=60)
    with patch.object(dispatcher, "post_message", posted):
        await dispatcher._attempt(claimed)
    posted.assert_not_called()


async def test_removed_refs_cannot_bypass_audit_scope():
    from types import SimpleNamespace

    from vera_shared.office.intake import _item

    qid = await _open()
    await _call(qid)
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (ROOM, TID))
        row.refs = [r for r in row.refs if r["ref"] not in
                    {SOURCE_ATTESTED, ATTEST_SCOPE_REF}]
    posted = AsyncMock()
    with pytest.raises(tasks.TaskBusy):
        await tasks.claim(room=ROOM, task_id=TID, agent="vera", lease_seconds=60)
    posted.assert_not_called()
    async with get_session() as s:
        task = await s.get(RoomTaskRow, (ROOM, TID))
    origin = SimpleNamespace(client_task_id="c", delivered_revision=0,
                             result_recipient={"channel": "dot"})
    assert (await _item(origin, task))["result_withheld"] is True


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


async def test_office_result_withheld_for_foreign_recipient():
    from types import SimpleNamespace

    from vera_shared.office.intake import _item
    await _open()
    async with get_session() as s:
        task = await s.get(RoomTaskRow, (ROOM, TID))
        task.refs = [*task.refs, {"kind": "source", "ref": SOURCE_ATTESTED},
                     {"kind": "source", "ref": ATTEST_SCOPE_REF}]
        task.status = "done"
    async with get_session() as s:
        task = await s.get(RoomTaskRow, (ROOM, TID))
    for channel, withheld in (("telegram", True), ("owner", False)):
        origin = SimpleNamespace(client_task_id="c", delivered_revision=0,
                                 result_recipient={"channel": channel})
        item = await _item(origin, task)
        assert item["result_withheld"] is withheld

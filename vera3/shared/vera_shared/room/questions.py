"""Трекер задач, шаг 3: вопросы владельцу.

ask блокирует задачу и ждёт владельца; answer (только дашборд) статус не трогает:
задача остаётся blocked, пока исполнитель не подтвердит ответ через ack.
Правка, событие журнала и сообщение комнаты — в одной транзакции.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskAnswerRow, RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room.messages import add_message
from vera_shared.room.question_view import list_questions, load_questions, question_dict
from vera_shared.room.task_events import record_event
from vera_shared.room.task_refs import validate_refs
from vera_shared.room.tasks import _locked, _require_lease
from vera_shared.timeutil import utc_naive_now

OWNER = "owner"
MAX_ANSWER_CHARS = 8000
MAX_QUESTION_CHARS = 4000


class QuestionNotFound(LookupError):
    def __init__(self, qid: int) -> None:
        super().__init__(f"question {qid} not found for this task")


class QuestionState(RuntimeError):
    """Вопрос в статусе, из которого это действие невозможно."""


def question_message_id(qid: int) -> str:
    return f"task-q-{qid}"


async def _question(s: AsyncSession, room: str, task_id: str, qid: int) -> RoomTaskQuestionRow:
    q = await s.get(RoomTaskQuestionRow, qid, with_for_update=True)
    if q is None or (q.room, q.task_id) != (room, task_id):
        raise QuestionNotFound(qid)
    return q


async def _unresolved(s: AsyncSession, room: str, task_id: str) -> int:
    return (await s.execute(select(func.count()).select_from(RoomTaskQuestionRow).where(
        RoomTaskQuestionRow.room == room, RoomTaskQuestionRow.task_id == task_id,
        RoomTaskQuestionRow.status.in_(("open", "answered"))))).scalar() or 0


async def _answers_of(s: AsyncSession, q: RoomTaskQuestionRow) -> list[RoomTaskAnswerRow]:
    return next(a for qq, a in await load_questions(s, q.room, q.task_id) if qq.qid == q.qid)


async def ask(*, room: str, task_id: str, agent: str, fencing_token: int, question: str,
              refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    question = question.strip()
    if not question or len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question must be 1..{MAX_QUESTION_CHARS} characters")
    clean_refs = validate_refs(refs) if refs is not None else None
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        q = RoomTaskQuestionRow(room=room, task_id=task_id, asked_by=agent,
                                question=question, status="open")
        s.add(q)
        await s.flush()
        row.status, row.owner, row.updated_at = "blocked", OWNER, utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind="question", agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=question,
                           data={"qid": q.qid, "refs": clean_refs})
        await add_message(s, room=room, message_id=question_message_id(q.qid),
                          from_agent=agent, task_id=task_id, status="question",
                          body=f"[вопрос #{q.qid} по {task_id}] {question}")
        await s.refresh(q)
        return question_dict(q, [])


async def answer(*, room: str, task_id: str, qid: int, text: str, by: str = OWNER,
                 ) -> tuple[dict[str, Any], bool]:
    """(вопрос, changed). Дословный повтор последнего ответа — не запись. Статус задачи не меняется."""
    text = text.strip()
    if not text or len(text) > MAX_ANSWER_CHARS:
        raise ValueError(f"answer must be 1..{MAX_ANSWER_CHARS} characters")
    async with get_session() as s:
        q = await _question(s, room, task_id, qid)
        if q.status not in ("open", "answered"):
            raise QuestionState(f"question {qid} is {q.status}; answers are closed")
        task = await s.get(RoomTaskRow, (room, task_id))
        if task is not None and task.status in ("done", "cancelled"):
            raise QuestionState(f"task {task_id!r} is {task.status}; answers are closed")
        prior = await _answers_of(s, q)
        if prior and prior[-1].text == text:
            return question_dict(q, prior), False
        row = RoomTaskAnswerRow(qid=qid, text=text, answered_by=by)
        s.add(row)
        q.status = "answered"
        await s.flush()
        await record_event(s, room=room, task_id=task_id, kind="answered", agent=by,
                           text=text, data={"qid": qid})
        await add_message(s, room=room, message_id=f"task-a-{row.id}", from_agent=by,
                          to_agent=q.asked_by, task_id=task_id, status="info",
                          in_reply_to=question_message_id(qid),
                          body=f"[ответ на вопрос #{qid} по {task_id}] {text}")
        await s.refresh(row)
        return question_dict(q, [*prior, row]), True


async def _close(s: AsyncSession, task: RoomTaskRow, q: RoomTaskQuestionRow, agent: str,
                 token: int, data: dict[str, Any], text: str | None = None) -> None:
    await s.flush()
    await record_event(s, room=q.room, task_id=q.task_id, kind="ack_answer", agent=agent,
                       session=task.holder_session, account=task.holder_account,
                       fencing_token=token, text=text, data=data)
    if task.status == "blocked" and await _unresolved(s, q.room, q.task_id) == 0:
        task.status, task.owner, task.updated_at = "in_progress", None, utc_naive_now()
        await record_event(s, room=q.room, task_id=q.task_id, kind="unblocked", agent=agent,
                           session=task.holder_session, account=task.holder_account,
                           fencing_token=token, data={"qid": q.qid})


async def ack(*, room: str, task_id: str, qid: int, agent: str, fencing_token: int,
              ) -> dict[str, Any]:
    async with get_session() as s:
        task = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                              fencing_token)
        q = await _question(s, room, task_id, qid)
        if q.status != "answered":
            raise QuestionState(f"question {qid} is {q.status}; only an answered one is acked")
        q.status, q.acked_at, q.ack_by = "acked", utc_naive_now(), agent
        await _close(s, task, q, agent, fencing_token, {"qid": qid})
        return question_dict(q, await _answers_of(s, q))


async def withdraw(*, room: str, task_id: str, qid: int, agent: str, fencing_token: int,
                   ) -> dict[str, Any]:
    async with get_session() as s:
        task = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                              fencing_token)
        q = await _question(s, room, task_id, qid)
        if q.asked_by != agent:
            raise QuestionState("only the agent who asked can withdraw a question")
        if q.status not in ("open", "answered"):
            raise QuestionState(f"question {qid} is {q.status}; nothing to withdraw")
        q.status = "withdrawn"
        await _close(s, task, q, agent, fencing_token, {"qid": qid, "withdrawn": True},
                     text="вопрос снят")
        return question_dict(q, await _answers_of(s, q))


async def questions_of(room: str, task_id: str) -> list[dict[str, Any]]:
    async with get_session() as s:
        return await list_questions(s, room, task_id)

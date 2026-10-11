"""Удержание неподтверждённой срочной задачи: вопрос владельцу и его снятие.

Тот же механизм вопросов, что `room.questions`, но без аренды: бот задачу не
берёт (иначе первым держателем в журнале стал бы он сам, и «Взял» ушло бы
про Веру). Статус задачи остаётся `open`: открытый вопрос и так не пускает
её в `room_task_next`, а после ответа она сразу доступна исполнителю.

Снимает удержание только ответ владельца (`answered_by == "owner"`, его
пишет лишь дашборд): ref `source_confirmed` и обычный next_action.
Оба шага идемпотентны — повтор, ретрай, перезапуск ничего не дублируют.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskAnswerRow, RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room.messages import add_message
from vera_shared.room.questions import OWNER, question_message_id
from vera_shared.room.task_events import record_event
from vera_shared.room.task_refs import validate_refs
from vera_shared.room.tasks import TaskNotFound, _locked
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.policy import (
    AGENT,
    ATTEST_SCOPE_REF,
    ATTESTED_ACTION,
    CONFIRMED_NOTE,
    SOURCE_ATTESTED,
    SOURCE_CONFIRMED,
    UNCERTAIN_QUESTION,
    attested_action,
)


def _our_question(room: str, task_id: str) -> object:
    q = RoomTaskQuestionRow
    return select(q.qid).where(q.room == room, q.task_id == task_id, q.asked_by == AGENT)


async def ask_owner_once(room: str, task_id: str) -> bool:
    """→ задан ли вопрос сейчас (False — уже был)."""
    async with get_session() as s:
        if await _locked(s, room, task_id) is None:
            raise TaskNotFound(room, task_id)
        if (await s.execute(_our_question(room, task_id).limit(1))).first():
            return False
        q = RoomTaskQuestionRow(room=room, task_id=task_id, asked_by=AGENT,
                                question=UNCERTAIN_QUESTION, status="open")
        s.add(q)
        await s.flush()
        await record_event(s, room=room, task_id=task_id, kind="question", agent=AGENT,
                           text=UNCERTAIN_QUESTION, data={"qid": q.qid})
        await add_message(s, room=room, message_id=question_message_id(q.qid),
                          from_agent=AGENT, task_id=task_id, status="question",
                          body=f"[вопрос #{q.qid} по {task_id}] {UNCERTAIN_QUESTION}")
        return True


def is_confirmed(refs: list[dict[str, str]] | None) -> bool:
    return any(r.get("ref") == SOURCE_CONFIRMED for r in refs or [])


async def lift_hold(room: str, task_id: str, action: str) -> bool:
    """Ответ владельца есть, а пометки ещё нет → снять удержание. → сняли ли сейчас."""
    a = RoomTaskAnswerRow
    async with get_session() as s:
        task = await _locked(s, room, task_id)
        if task is None or is_confirmed(task.refs):
            return False
        answered = (await s.execute(
            select(a.id).where(a.qid.in_(_our_question(room, task_id)),
                               a.answered_by == OWNER).limit(1))).first()
        if answered is None:
            return False
        task.refs = validate_refs([*(task.refs or []),
                                   {"kind": "source", "ref": SOURCE_CONFIRMED}])
        task.next_action = f"{CONFIRMED_NOTE}\n{action}"[:2000]
        task.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind="progress", agent=AGENT,
                           text=CONFIRMED_NOTE, data={"ref": SOURCE_CONFIRMED})
        return True


async def lift_attested(s: AsyncSession, task: RoomTaskRow, reply: str) -> None:
    """Подтверждение через dot: ref `source_attested_by_dot` и ограниченный next_action.

    `source_confirmed` не ставится: исполнитель должен видеть, что это не слово владельца.
    Пишет в транзакции вызывающего (задача уже под блокировкой).
    """
    have = {r.get("ref") for r in task.refs or []}
    add = [{"kind": "source", "ref": r} for r in (SOURCE_ATTESTED, ATTEST_SCOPE_REF)
           if r not in have]
    if add:
        task.refs = validate_refs([*(task.refs or []), *add])
    task.auto_pickup = False
    task.next_action = attested_action(reply)
    task.updated_at = utc_naive_now()
    await record_event(s, room=task.room, task_id=task.task_id, kind="progress", agent=AGENT,
                       text=ATTESTED_ACTION, data={"ref": SOURCE_ATTESTED})

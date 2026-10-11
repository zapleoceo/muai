"""Ответ владельца реплаем в Telegram на «Открыл задачу …» неподтверждённой задачи.

Подлинность `from.id` обеспечивает транспорт (бот на long polling: апдейты идут
из Bot API по токену, id проставляет Telegram) и проверка владельца в боте; здесь —
только состояние: ссылка «сообщение → вопрос» одноразовая и привязана к ревизии
задачи (id последнего события журнала на момент отправки).

Ответ «да» пишется как ответ владельца на вопрос об авторстве, «нет» — как «нет»
и задачу не освобождает (статус blocked, держатель — владелец). Происхождение —
событие `owner_confirmed`. Тексты dot/relay сюда не попадают: путь один — реплай
от проверенного `from.id`.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskQuestionRow, RoomTaskRow
from vera_shared.db.models_voice import OwnerReplyLinkRow
from vera_shared.room.questions import OWNER, QuestionNotFound, QuestionState, answer
from vera_shared.room.task_events import record_event
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.policy import AGENT

YES_TEXT = "да"
NO_TEXT = "нет"
_YES = frozenset({"да", "сказал", "я", "это я", "yes"})
_NO = frozenset({"нет", "не я", "no"})

#: ignored — не наша ссылка/чат; unknown — текст вне белого списка; yes/no — записано;
#: used — уже учтено; stale — задача ушла вперёд; closed — вопрос уже не открыт.
Outcome = Literal["ignored", "unknown", "yes", "no", "used", "stale", "closed"]

HINT_TEXT = "Ответьте реплаем: «да» (это я сказал) или «нет» (не я)."
USED_TEXT = "Уже учтено."
STALE_TEXT = "Устарело, ответьте в дашборде."
YES_REPLY = "Принято: источник подтверждён."
NO_REPLY = "Принято: не вы. Задача остаётся на удержании."


def normalize_reply(text: str | None) -> str | None:
    """→ YES_TEXT / NO_TEXT или None, если фраза не из белого списка."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .!")
    if t in _YES:
        return YES_TEXT
    return NO_TEXT if t in _NO else None


def is_denial(text: str) -> bool:
    return normalize_reply(text) == NO_TEXT


async def _revision(s: AsyncSession, room: str, task_id: str) -> int:
    return (await s.execute(select(func.max(RoomTaskEventRow.id)).where(
        RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id))).scalar() or 0


async def open_question_qid(room: str, task_id: str) -> int | None:
    q = RoomTaskQuestionRow
    async with get_session() as s:
        return (await s.execute(select(q.qid).where(
            q.room == room, q.task_id == task_id, q.asked_by == AGENT,
            q.status == "open").order_by(q.qid).limit(1))).scalar_one_or_none()


async def current_revision(room: str, task_id: str) -> int:
    async with get_session() as s:
        return await _revision(s, room, task_id)


async def link_message(*, message_id: int, chat_id: int, room: str, task_id: str,
                       qid: int, revision: int) -> None:
    async with get_session() as s:
        if await s.get(OwnerReplyLinkRow, message_id) is None:
            s.add(OwnerReplyLinkRow(message_id=message_id, chat_id=chat_id, room=room,
                                    task_id=task_id, qid=qid, revision=revision))


async def has_link(message_id: int) -> bool:
    async with get_session() as s:
        return await s.get(OwnerReplyLinkRow, message_id) is not None


async def confirm_by_reply(*, chat_id: int, link_message_id: int, reply_message_id: int,
                           from_id: int, text: str | None) -> Outcome:
    """Вызывать только после проверки в боте: приватный чат владельца, from_id == owner."""
    canonical = normalize_reply(text)
    if canonical is None:
        return "unknown"
    now = utc_naive_now()
    async with get_session() as s:
        link = await s.get(OwnerReplyLinkRow, link_message_id, with_for_update=True)
        if link is None or link.chat_id != chat_id:
            return "ignored"
        if link.used_at is not None:
            return "used"
        room, task_id = link.room, link.task_id
        if await _revision(s, room, task_id) != link.revision:
            return "stale"
        try:
            _, changed = await answer(room=room, task_id=task_id, qid=link.qid,
                                      text=canonical, by=OWNER, s=s)
        except (QuestionState, QuestionNotFound):
            return "closed"
        if not changed:
            return "used"
        await record_event(
            s, room=room, task_id=task_id, kind="owner_confirmed", agent=OWNER,
            text=canonical, data={
                "telegram_message_id": link_message_id,
                "reply_message_id": reply_message_id, "from_id": from_id,
                "at": now.isoformat(), "revision": link.revision, "qid": link.qid})
        if canonical == NO_TEXT:
            await _hold_denied(s, room, task_id, now)
        link.used_at = now
    return "yes" if canonical == YES_TEXT else "no"


async def _hold_denied(s: AsyncSession, room: str, task_id: str, now: datetime) -> None:
    # Ответ закрывает вопрос и тем отпустил бы задачу в room_task_next — «нет» её держит.
    task = await s.get(RoomTaskRow, (room, task_id), with_for_update=True)
    if task is not None and task.status == "open":
        task.status, task.owner, task.updated_at = "blocked", OWNER, now
        await record_event(s, room=room, task_id=task_id, kind="blocked", agent=AGENT,
                           text="Владелец: источник не он — удержание сохранено.")

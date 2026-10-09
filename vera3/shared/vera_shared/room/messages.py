"""Сообщения комнаты агентов: запись с идемпотентностью и входящий поток с курсором.

Курсор полагается на то, что внутри комнаты порядок id совпадает с порядком
фиксации. BIGSERIAL этого не гарантирует: две транзакции берут id 1 и 2, вторая
коммитится первой, читатель видит 2, подтверждает его — и id=1 после своего
коммита уже никогда не попадёт во входящие. Поэтому запись в комнату
сериализуется advisory-локом транзакции до INSERT (на SQLite запись и так одна).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomCursorRow, RoomMessageRow
from vera_shared.timeutil import utc_naive_now

MESSAGE_STATUSES = ("info", "request", "ack", "question", "in_progress", "done", "blocked")
DEFAULT_CONSUMER = "default"


class MessageConflict(ValueError):
    """message_id уже занят другим автором или другим содержимым."""


def message_dict(r: RoomMessageRow) -> dict[str, Any]:
    return {"id": r.id, "room": r.room, "message_id": r.message_id, "from": r.from_agent,
            "session": r.from_session, "to": r.to_agent, "task_id": r.task_id,
            "in_reply_to": r.in_reply_to, "status": r.status, "body": r.body,
            "at": r.created_at.isoformat()}


async def _lock_room(s: AsyncSession, room: str) -> None:
    if s.bind.dialect.name == "postgresql":
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                        {"k": f"room_messages:{room}"})


async def add_message(s: AsyncSession, *, room: str, message_id: str, from_agent: str,
                      body: str, to_agent: str | None = None, task_id: str | None = None,
                      in_reply_to: str | None = None, status: str = "info") -> RoomMessageRow:
    """Запись в транзакции вызывающего: сообщение фиксируется вместе с его правкой."""
    if status not in MESSAGE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(MESSAGE_STATUSES)}")
    await _lock_room(s, room)
    row = RoomMessageRow(room=room, message_id=message_id, from_agent=from_agent, body=body,
                         to_agent=to_agent, task_id=task_id, in_reply_to=in_reply_to,
                         status=status)
    s.add(row)
    await s.flush()
    return row


async def _existing(room: str, message_id: str) -> RoomMessageRow | None:
    async with get_session() as s:
        return (await s.execute(select(RoomMessageRow).where(
            RoomMessageRow.room == room, RoomMessageRow.message_id == message_id,
        ))).scalar_one_or_none()


def _same_payload(r: RoomMessageRow, payload: dict[str, Any]) -> bool:
    return all(getattr(r, k) == v for k, v in payload.items())


async def post_message(*, room: str, message_id: str, from_agent: str, body: str,
                       to_agent: str | None = None, task_id: str | None = None,
                       in_reply_to: str | None = None, status: str = "info",
                       from_session: str | None = None) -> tuple[dict[str, Any], bool]:
    """(сообщение, deduped). Повтор того же message_id с тем же содержимым — дубль, не ошибка."""
    if status not in MESSAGE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(MESSAGE_STATUSES)}")
    payload = {"from_agent": from_agent, "body": body, "to_agent": to_agent,
               "task_id": task_id, "in_reply_to": in_reply_to, "status": status}
    try:
        async with get_session() as s:
            await _lock_room(s, room)
            row = RoomMessageRow(room=room, message_id=message_id,
                                 from_session=from_session, **payload)
            s.add(row)
            await s.flush()
            await s.refresh(row)
            return message_dict(row), False
    except IntegrityError:
        prior = await _existing(room, message_id)
        if prior is None:
            raise
        if not _same_payload(prior, payload):
            raise MessageConflict(
                f"message_id {message_id!r} already used by {prior.from_agent} "
                "with a different payload") from None
        return message_dict(prior), True


async def _cursor(agent: str, room: str, consumer: str) -> int:
    async with get_session() as s:
        row = await s.get(RoomCursorRow, (agent, room, consumer))
        return row.last_message_id if row else 0


async def ack(*, agent: str, room: str, consumer: str, up_to_id: int) -> int:
    """Подтвердить обработку до up_to_id включительно. Курсор только растёт и не
    уходит дальше последнего существующего сообщения комнаты."""
    async with get_session() as s:
        last = (await s.execute(select(func.max(RoomMessageRow.id)).where(
            RoomMessageRow.room == room))).scalar() or 0
        row = await s.get(RoomCursorRow, (agent, room, consumer), with_for_update=True)
        if row is None:
            row = RoomCursorRow(agent=agent, room=room, consumer=consumer, last_message_id=0)
            s.add(row)
        row.last_message_id = max(row.last_message_id or 0, min(up_to_id, last))
        row.updated_at = utc_naive_now()
        return row.last_message_id


async def inbox(*, agent: str, room: str, consumer: str, limit: int,
                since_id: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """Чужие сообщения мне или всем после курсора (или since_id) и сам курсор. Курсор
    не двигает: подтверждение — отдельный `ack` после обработки."""
    cursor = await _cursor(agent, room, consumer)
    after = cursor if since_id is None else since_id
    async with get_session() as s:
        rows = (await s.execute(
            select(RoomMessageRow).where(
                RoomMessageRow.room == room, RoomMessageRow.id > after,
                RoomMessageRow.from_agent != agent,
                or_(RoomMessageRow.to_agent.is_(None), RoomMessageRow.to_agent == agent),
            ).order_by(RoomMessageRow.id).limit(limit)
        )).scalars().all()
    return [message_dict(r) for r in rows], cursor


async def history(*, room: str, limit: int, before_id: int | None = None,
                  task_id: str | None = None) -> list[dict[str, Any]]:
    q = select(RoomMessageRow).where(RoomMessageRow.room == room)
    if before_id is not None:
        q = q.where(RoomMessageRow.id < before_id)
    if task_id is not None:
        q = q.where(RoomMessageRow.task_id == task_id)
    async with get_session() as s:
        rows = (await s.execute(q.order_by(RoomMessageRow.id.desc()).limit(limit))
                ).scalars().all()
    return [message_dict(r) for r in reversed(rows)]

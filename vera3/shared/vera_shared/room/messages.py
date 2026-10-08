"""Сообщения комнаты агентов: запись с идемпотентностью и входящий поток с курсором."""
from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomCursorRow, RoomMessageRow
from vera_shared.timeutil import utc_naive_now

MESSAGE_STATUSES = ("info", "request", "ack", "question", "in_progress", "done", "blocked")


def message_dict(r: RoomMessageRow) -> dict[str, Any]:
    return {"id": r.id, "room": r.room, "message_id": r.message_id, "from": r.from_agent,
            "to": r.to_agent, "task_id": r.task_id, "in_reply_to": r.in_reply_to,
            "status": r.status, "body": r.body, "at": r.created_at.isoformat()}


async def _existing(room: str, message_id: str) -> RoomMessageRow | None:
    async with get_session() as s:
        return (await s.execute(select(RoomMessageRow).where(
            RoomMessageRow.room == room, RoomMessageRow.message_id == message_id,
        ))).scalar_one_or_none()


async def post_message(*, room: str, message_id: str, from_agent: str, body: str,
                       to_agent: str | None = None, task_id: str | None = None,
                       in_reply_to: str | None = None, status: str = "info",
                       ) -> tuple[dict[str, Any], bool]:
    """(сообщение, deduped). Повтор message_id от того же автора — не ошибка, а дубль."""
    if status not in MESSAGE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(MESSAGE_STATUSES)}")
    try:
        async with get_session() as s:
            row = RoomMessageRow(room=room, message_id=message_id, from_agent=from_agent,
                                 to_agent=to_agent, task_id=task_id,
                                 in_reply_to=in_reply_to, status=status, body=body)
            s.add(row)
            await s.flush()
            await s.refresh(row)
            return message_dict(row), False
    except IntegrityError:
        prior = await _existing(room, message_id)
        if prior is None:
            raise
        if prior.from_agent != from_agent:
            raise ValueError(f"message_id {message_id!r} already used by {prior.from_agent}"
                             ) from None
        return message_dict(prior), True


async def _cursor(agent: str, room: str) -> int:
    async with get_session() as s:
        row = await s.get(RoomCursorRow, (agent, room))
        return row.last_message_id if row else 0


async def advance_cursor(agent: str, room: str, last_id: int) -> int:
    """Курсор только растёт: подтверждение старого id не откатывает прочитанное."""
    async with get_session() as s:
        row = await s.get(RoomCursorRow, (agent, room), with_for_update=True)
        if row is None:
            row = RoomCursorRow(agent=agent, room=room, last_message_id=0)
            s.add(row)
        row.last_message_id = max(row.last_message_id or 0, last_id)
        row.updated_at = utc_naive_now()
        return row.last_message_id


async def inbox(*, agent: str, room: str, limit: int, since_id: int | None = None,
                ) -> tuple[list[dict[str, Any]], int]:
    """Чужие сообщения мне или всем после курсора (или since_id) и текущий курсор."""
    after = await _cursor(agent, room) if since_id is None else since_id
    async with get_session() as s:
        rows = (await s.execute(
            select(RoomMessageRow).where(
                RoomMessageRow.room == room, RoomMessageRow.id > after,
                RoomMessageRow.from_agent != agent,
                or_(RoomMessageRow.to_agent.is_(None), RoomMessageRow.to_agent == agent),
            ).order_by(RoomMessageRow.id).limit(limit)
        )).scalars().all()
    return [message_dict(r) for r in rows], after


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

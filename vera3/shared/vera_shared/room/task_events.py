"""Журнал событий задач комнаты: только дописывается, правки и удаления нет."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_room import EVENT_KINDS, RoomTaskEventRow


async def record_event(
    db: AsyncSession, *, room: str, task_id: str, kind: str, agent: str | None = None,
    session: str | None = None, account: str | None = None,
    fencing_token: int | None = None, text: str | None = None,
    data: dict[str, Any] | None = None,
) -> RoomTaskEventRow:
    """Пишет в транзакции вызывающего: событие фиксируется вместе с правкой задачи."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown task event kind {kind!r}")
    row = RoomTaskEventRow(room=room, task_id=task_id, kind=kind, agent=agent,
                           session=session, account=account,
                           fencing_token=fencing_token, text=text, data=data)
    db.add(row)
    await db.flush()
    return row


def event_dict(r: RoomTaskEventRow) -> dict[str, Any]:
    return {"id": r.id, "room": r.room, "task_id": r.task_id, "at": r.at.isoformat(),
            "kind": r.kind, "agent": r.agent, "session": r.session, "account": r.account,
            "fencing_token": r.fencing_token, "text": r.text, "data": r.data}


async def list_events(db: AsyncSession, *, room: str, task_id: str,
                      kinds: tuple[str, ...] | None = None, limit: int = 100,
                      since_id: int | None = None) -> list[dict[str, Any]]:
    q = select(RoomTaskEventRow).where(RoomTaskEventRow.room == room,
                                       RoomTaskEventRow.task_id == task_id)
    if kinds:
        q = q.where(RoomTaskEventRow.kind.in_(kinds))
    if since_id is not None:
        q = q.where(RoomTaskEventRow.id > since_id)
    rows = (await db.execute(q.order_by(RoomTaskEventRow.id).limit(limit))).scalars().all()
    return [event_dict(r) for r in rows]

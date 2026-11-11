"""Заведение задачи комнаты без аренды (`open_task`)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room.task_events import record_event
from vera_shared.room.task_fields import validate_open_fields
from vera_shared.room.task_store import _locked, get_task_row
from vera_shared.room.task_view import task_dict


async def open_task(*, room: str, task_id: str, agent: str, title: str | None,
                    paths: list[str] | None, project: str | None = None,
                    priority: int | None = None, auto_pickup: bool | None = None,
                    depends_on: list[str] | None = None, next_action: str | None = None,
                    refs: list[dict[str, Any]] | None = None,
                    responsible: str | None = None) -> tuple[dict[str, Any], bool]:
    """(задача, created). Существующая возвращается как есть, поля очереди не меняются."""
    extra = validate_open_fields(task_id, project=project, priority=priority,
                                 auto_pickup=auto_pickup, depends_on=depends_on,
                                 next_action=next_action, refs=refs,
                                 responsible=responsible)
    try:
        async with get_session() as s:
            row = await _locked(s, room, task_id)
            if row is not None:
                return task_dict(row), False
            row = RoomTaskRow(room=room, task_id=task_id, title=title, created_by=agent,
                              status="open", fencing_token=0, paths=paths or [], **extra)
            s.add(row)
            await record_event(s, room=room, task_id=task_id, kind="created", agent=agent,
                               text=title)
            await s.refresh(row)
            return task_dict(row), True
    except IntegrityError:
        async with get_session() as s:
            row = await get_task_row(s, room, task_id)
            if row is None:
                raise
            return task_dict(row), False

"""Office intake: общие ошибки, настройки комнаты и проверка «задача своя»."""
from __future__ import annotations

import os
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.office import repository as repo
from vera_shared.office.acl import OfficeForbidden, OfficePrincipal, require_own
from vera_shared.office.intake_input import ID_RE

TASK_PREFIX = "vb-"
CLOSED = ("done", "cancelled")


class IntakeNotFound(LookupError):
    """Задачи или вопроса нет."""


class IntakeConflict(RuntimeError):
    """Повтор с другим содержимым, устаревшая ревизия или закрытая задача (HTTP 409)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class IntakeBusy(RuntimeError):
    """Параллельное создание того же client_task_id ещё идёт: повторить (HTTP 503)."""


def office_room() -> str:
    return os.environ.get("OFFICE_ROOM", "main")


def auto_pickup() -> bool:
    """Задачи приёма не берутся автоматически, пока флаг не включён."""
    return os.environ.get("OFFICE_AUTO_PICKUP", "0").lower() in ("1", "true", "yes")


async def own_task(principal: OfficePrincipal, room: str, task_id: str,
                   ) -> tuple[Any, RoomTaskRow]:
    if not ID_RE.match(task_id):
        raise IntakeNotFound("task not found")
    async with get_session() as s:
        origin = await repo.origin_of(s, task_id)
        task = await s.get(RoomTaskRow, (origin.room, task_id)) if origin else None
    if origin is None or task is None:
        raise IntakeNotFound("task not found")
    try:
        require_own(principal, creator=origin.creator_principal, project=task.project)
    except OfficeForbidden:
        raise IntakeNotFound("task not found") from None  # чужое неотличимо от несуществующего
    return origin, task

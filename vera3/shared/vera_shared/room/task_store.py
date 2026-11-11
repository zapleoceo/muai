"""SQL задач комнаты: строка задачи под блокировкой и проверки квитанций."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow


async def _locked(s: Any, room: str, task_id: str) -> RoomTaskRow | None:
    return await s.get(RoomTaskRow, (room, task_id), with_for_update=True)


async def get_task_row(s: Any, room: str, task_id: str) -> RoomTaskRow | None:
    return await s.get(RoomTaskRow, (room, task_id))


async def last_done_event(s: Any, room: str, task_id: str) -> RoomTaskEventRow | None:
    return (await s.execute(select(RoomTaskEventRow).where(
        RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id,
        RoomTaskEventRow.kind == "done").order_by(RoomTaskEventRow.id.desc()).limit(1)
    )).scalar_one_or_none()


async def lock_receipt(s: Any, receipt_id: str) -> None:
    # Serialize receipt reuse checks across different task rows without a new table.
    if s.bind.dialect.name == "postgresql":
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": f"attested-receipt:{receipt_id}"})


async def receipt_already_used(s: Any, receipt_id: str) -> bool:
    return (await s.execute(select(RoomTaskEventRow.id).where(
        RoomTaskEventRow.kind == "done",
        RoomTaskEventRow.data["receipt_id"].as_string() == receipt_id
    ).limit(1))).first() is not None

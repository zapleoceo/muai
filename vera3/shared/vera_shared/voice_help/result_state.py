"""Доставка результата исполнителя срочной задачи владельцу — без миграции.

Хранилище — существующая строка `voice_command_queue`: `help_state` (VARCHAR(16), без
CHECK) несёт этап, `source` (JSONB «откуда») — квитанцию. Этапы:

    taken | opened | … ─намерение─▶ result_pending ─отправка ок─▶ closed (+квитанция)
                                          │ ─Telegram отказал─▶ result_error ─повтор─▶ …
                                          └─ намерение без квитанции на след. проходе
                                             (упали между отправкой и записью)
                                             ─▶ result_unknown (не повторяем, ждёт человека)

Намерение фиксируется отдельной транзакцией ДО отправки; вслепую повторяется только
точный отказ (исключение отправки), неизвестный исход — никогда.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow
from vera_shared.db.models_voice import VoiceCommandRow

PENDING, ERROR, UNKNOWN = "result_pending", "result_error", "result_unknown"
RESULT_WATCHED = ("taken", PENDING, ERROR)
RESULT_MAX_ERRORS = 5
#: Результаты, оформленные до выкатки, не досылаем (строки закрывались без них).
RESULT_DELIVERY_SINCE = datetime(2026, 10, 27, 19, 22)


async def executor_result(room: str, task_id: str) -> tuple[str, str] | None:
    """→ (агент, текст) последнего done после RESULT_DELIVERY_SINCE; иначе None."""
    ev = RoomTaskEventRow
    async with get_session() as s:
        row = (await s.execute(
            select(ev.agent, ev.text, ev.at).where(
                ev.room == room, ev.task_id == task_id, ev.kind == "done")
            .order_by(ev.id.desc()).limit(1))).first()
    if row is None or not (row.text or "").strip() or row.at < RESULT_DELIVERY_SINCE:
        return None
    return row.agent, row.text.strip()


async def move(command_id: str, from_states: tuple[str, ...], to: str, now: datetime) -> bool:
    """Условный переход этапа; → True, если именно мы его сделали."""
    async with get_session() as s:
        res = await s.execute(update(VoiceCommandRow)
                              .where(VoiceCommandRow.command_id == command_id,
                                     VoiceCommandRow.help_state.in_(from_states))
                              .values(help_state=to, updated_at=now))
        return res.rowcount == 1


def result_errors(row: VoiceCommandRow) -> int:
    return int((row.source or {}).get("result_errors", 0))


async def _merge_source(command_id: str, **extra: Any) -> dict[str, Any]:
    async with get_session() as s:
        cur = await s.get(VoiceCommandRow, command_id)
        return {**(cur.source or {}), **extra} if cur else extra


async def record_error(command_id: str, errors: int, now: datetime) -> None:
    merged = await _merge_source(command_id, result_errors=errors)
    async with get_session() as s:
        await s.execute(update(VoiceCommandRow)
                        .where(VoiceCommandRow.command_id == command_id,
                               VoiceCommandRow.help_state == PENDING)
                        .values(help_state=ERROR, source=merged, updated_at=now))


async def record_receipt(command_id: str, message_id: int, now: datetime) -> bool:
    """Квитанция и закрытие одним UPDATE; → True, если записана именно сейчас."""
    merged = await _merge_source(command_id, result_message_id=message_id,
                                 result_sent_at=now.isoformat())
    async with get_session() as s:
        res = await s.execute(update(VoiceCommandRow)
                              .where(VoiceCommandRow.command_id == command_id,
                                     VoiceCommandRow.help_state == PENDING)
                              .values(help_state="closed", source=merged, updated_at=now))
        return res.rowcount == 1

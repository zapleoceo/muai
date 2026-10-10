"""Путь срочной просьбы в строке очереди (`voice_command_queue.help_state`).

    ready ──open──▶ opened ─5 мин─▶ escalated
                      │ ─20 мин─▶ reminded
                      └─claim─▶ taken

Подтверждения «Это ты сказал?» больше нет (11.10.2026): задача открывается
сразу, неуверенный источник помечается в ней самой. Строки `confirm`/`asked`
от прежней версии бот открывает так же, как `ready`. Отметки «сообщили
владельцу» — колонки, а не лог: после перезапуска бот видит, что уже сказано.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models_voice import VoiceCommandRow

WATCHED = ("opened", "escalated", "reminded")
#: `status` строки очереди, пока задачу никто не взял: не `pending` (бот взял
#: бы её снова) и не `done` — закрывает её `release_held` на claim/отмене.
HELD = "opened"


async def _set(command_id: str, now: datetime, **values: object) -> None:
    async with get_session() as s:
        await s.execute(update(VoiceCommandRow)
                        .where(VoiceCommandRow.command_id == command_id)
                        .values(updated_at=now, **values))


async def mark_opened(command_id: str, task_id: str, now: datetime) -> None:
    await _set(command_id, now, help_state="opened", task_id=task_id, task_opened_at=now)


async def hold_command(command_id: str, now: datetime) -> None:
    await _set(command_id, now, status=HELD, error=None)


async def release_held(command_id: str, now: datetime) -> None:
    """Задачу взяли или закрыли — строка очереди готова (текст живёт в событии)."""
    async with get_session() as s:
        await s.execute(update(VoiceCommandRow)
                        .where(VoiceCommandRow.command_id == command_id,
                               VoiceCommandRow.status == HELD)
                        .values(status="done", instruction="", updated_at=now))


async def watched_help() -> list[VoiceCommandRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(VoiceCommandRow)
            .where(VoiceCommandRow.help_state.in_(WATCHED),
                   VoiceCommandRow.task_id.is_not(None))
            .order_by(VoiceCommandRow.created_at)
        )).scalars())


async def mark_taken(command_id: str, agent: str, now: datetime) -> None:
    await _set(command_id, now, help_state="taken", taken_by=agent[:64], taken_at=now)


async def mark_escalated(command_id: str, now: datetime) -> None:
    await _set(command_id, now, help_state="escalated", escalated_at=now)


async def mark_reminded(command_id: str, now: datetime) -> None:
    await _set(command_id, now, help_state="reminded", reminded_at=now)


async def mark_closed(command_id: str, now: datetime) -> None:
    """Задачу закрыли, так никто и не взяв (отменил владелец) — следить нечего."""
    await _set(command_id, now, help_state="closed")

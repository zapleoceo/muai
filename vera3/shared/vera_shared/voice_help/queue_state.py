"""Путь срочной просьбы в строке очереди (`voice_command_queue.help_state`).

    confirm ──ask──▶ asked ──«Да»──▶ ready ──open──▶ opened ─5 мин─▶ escalated
       (сомнение)      │ «Нет» → declined                │ ─20 мин─▶ reminded
                       │ 10 мин → expired                └─claim─▶ taken
    ready — сразу, если уверенность высокая.

Пока владелец не ответил, строка стоит в `status='waiting'`: `claim_command`
её не берёт, `revive_stale` не трогает. Все переходы — под блокировкой
строки, поэтому двойное нажатие «Да» или «Да» вдогонку истечению ничего не
задваивает. Отметки «сообщили владельцу» — колонки, а не лог: после
перезапуска бот видит, что уже сказано.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.voice_help.policy import CONFIRM_TTL

WATCHED = ("opened", "escalated", "reminded")


async def _set(command_id: str, now: datetime, **values: object) -> None:
    async with get_session() as s:
        await s.execute(update(VoiceCommandRow)
                        .where(VoiceCommandRow.command_id == command_id)
                        .values(updated_at=now, **values))


async def mark_asked(command_id: str, now: datetime) -> None:
    await _set(command_id, now, status="waiting", help_state="asked",
               confirm_asked_at=now)


async def answer_confirmation(command_id: str, yes: bool, now: datetime) -> str:
    """→ confirmed | declined | expired | unknown (не ждали ответа: уже решено)."""
    async with get_session() as s:
        row = await s.get(VoiceCommandRow, command_id, with_for_update=True)
        if row is None or row.help_state != "asked" or row.status != "waiting":
            return "unknown"
        row.updated_at = now
        if row.confirm_asked_at is None or now - row.confirm_asked_at > CONFIRM_TTL:
            row.status, row.help_state, row.instruction = "done", "expired", ""
            return "expired"
        if not yes:
            row.status, row.help_state, row.instruction = "done", "declined", ""
            return "declined"
        row.status, row.help_state = "pending", "ready"
        # Попытки считаются заново: вопрос владельцу — не попытка исполнения.
        row.attempts, row.next_attempt_at, row.error = 0, None, None
        return "confirmed"


async def expired_asks(now: datetime) -> list[str]:
    async with get_session() as s:
        return list((await s.execute(
            select(VoiceCommandRow.command_id)
            .where(VoiceCommandRow.help_state == "asked",
                   VoiceCommandRow.status == "waiting",
                   VoiceCommandRow.confirm_asked_at < now - CONFIRM_TTL)
        )).scalars())


async def mark_expired(command_id: str, now: datetime) -> bool:
    """True — истекло сейчас; False — владелец успел ответить."""
    async with get_session() as s:
        done = (await s.execute(
            update(VoiceCommandRow)
            .where(VoiceCommandRow.command_id == command_id,
                   VoiceCommandRow.help_state == "asked",
                   VoiceCommandRow.status == "waiting")
            .values(status="done", help_state="expired", instruction="", updated_at=now)
        )).rowcount
    return bool(done)


async def mark_opened(command_id: str, task_id: str, now: datetime) -> None:
    await _set(command_id, now, help_state="opened", task_id=task_id, task_opened_at=now)


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

"""Голосовые поручения владельца: запись (шлюз) и очередь исполнения (бот).

Шина — та же Postgres-таблица-очередь, что у сессий Claude Code
(`claude_session_queue`): шлюз и бот уже оба ходят в одну базу, а второй
брокер (Redis, HTTP-колбэк в бот) означал бы второй путь доставки, который
надо отдельно поднимать, мониторить и защищать. Поручение одновременно
пишется событием — чтобы было видно в истории и в поиске, как любая реплика.

Одна транзакция на оба INSERT: событие без строки очереди — поручение, на
которое никто не ответит, строка без события — ответ, которого нет в истории.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

SOURCE = "voice_command"
#: Три попытки, потом error: гонять поиск по кругу на ядовитом поручении
#: дороже, чем один раз не ответить — владелец увидит, что ответа нет.
MAX_ATTEMPTS = 3
#: Бот перезапустили посреди ответа. Поиск отвечает до 120с — порог выше.
STALE_MINUTES = 10


def event_text(instruction: str) -> str:
    return f"Голосовое поручение Вере: {instruction}"


async def create_command(command_id: str, instruction: str, spoken_at: datetime,
                         *, app: str | None, window_title: str | None,
                         ) -> tuple[int | None, bool]:
    """→ (event_id, повтор ли это). Повтор той же команды ничего не пишет."""
    async with get_session() as s:
        existing = (await s.execute(
            select(VoiceCommandRow.event_id)
            .where(VoiceCommandRow.command_id == command_id)
        )).first()
        if existing is not None:
            return existing[0], True
        event = EventRow(
            source=SOURCE, source_event_id=command_id, account="laptop",
            category="command", content_text=event_text(instruction),
            occurred_at=spoken_at,
            metadata_={"app": app, "window_title": window_title,
                       "author_role": "self", "author_label": "Я"},
            triage_status="pending",
        )
        s.add(event)
        await s.flush()
        s.add(VoiceCommandRow(command_id=command_id, event_id=event.id,
                              instruction=instruction, spoken_at=spoken_at,
                              status="pending"))
        try:
            await s.flush()
        except IntegrityError:
            # Два ретрая одной команды разминулись с проверкой выше. Откат
            # уносит и событие — второе не нужно, первое уже записано.
            await s.rollback()
            return None, True
        return event.id, False


async def claim_command() -> VoiceCommandRow | None:
    async with get_session() as s:
        chosen = (await s.execute(
            select(VoiceCommandRow.command_id)
            .where(VoiceCommandRow.status == "pending")
            .order_by(VoiceCommandRow.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if chosen is None:
            return None
        return (await s.execute(
            update(VoiceCommandRow)
            .where(VoiceCommandRow.command_id == chosen)
            .values(status="processing", attempts=VoiceCommandRow.attempts + 1,
                    updated_at=utc_naive_now())
            .returning(VoiceCommandRow)
        )).scalar_one_or_none()


async def _set(command_id: str, **values: object) -> None:
    async with get_session() as s:
        await s.execute(
            update(VoiceCommandRow)
            .where(VoiceCommandRow.command_id == command_id)
            .values(updated_at=utc_naive_now(), **values)
        )


async def mark_acked(command_id: str) -> None:
    await _set(command_id, acked_at=utc_naive_now())


async def finish_command(command_id: str) -> None:
    # Текст поручения живёт в событии; в очереди он больше не нужен.
    await _set(command_id, status="done", instruction="", error=None)


async def fail_command(command_id: str, reason: str, attempts: int) -> str:
    status = "error" if attempts >= MAX_ATTEMPTS else "pending"
    await _set(command_id, status=status, error=reason[:500])
    return status


async def revive_stale() -> int:
    async with get_session() as s:
        result = await s.execute(
            update(VoiceCommandRow)
            .where(VoiceCommandRow.status == "processing",
                   VoiceCommandRow.updated_at
                   < utc_naive_now() - timedelta(minutes=STALE_MINUTES))
            .values(status="pending", updated_at=utc_naive_now())
        )
        return result.rowcount or 0

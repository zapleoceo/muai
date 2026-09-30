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

from sqlalchemy import and_, or_, select, update
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
#: Первая пауза между попытками; дальше удваивается.
RETRY_BASE_S = 30
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


def retry_delay(attempts: int) -> timedelta:
    """Пауза перед следующей попыткой: 30 с, минута, две. Три подряд без паузы
    — это три отказа одного и того же упавшего поиска за секунду."""
    return timedelta(seconds=RETRY_BASE_S * 2 ** max(0, attempts - 1))


async def claim_command() -> VoiceCommandRow | None:
    now = utc_naive_now()
    async with get_session() as s:
        chosen = (await s.execute(
            select(VoiceCommandRow.command_id)
            .where(VoiceCommandRow.status == "pending",
                   VoiceCommandRow.attempts < MAX_ATTEMPTS,
                   or_(VoiceCommandRow.next_attempt_at.is_(None),
                       VoiceCommandRow.next_attempt_at <= now))
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
                    updated_at=now)
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


async def mark_answered(command_id: str) -> None:
    """Ответ ушёл в Telegram. Отдельной записью сразу после отправки: упади
    процесс до finish — перезапуск увидит признак и второй раз не ответит."""
    await _set(command_id, answered_at=utc_naive_now())


async def finish_command(command_id: str) -> None:
    # Текст поручения живёт в событии; в очереди он больше не нужен.
    await _set(command_id, status="done", instruction="", error=None)


async def fail_command(command_id: str, reason: str, attempts: int) -> str:
    if attempts >= MAX_ATTEMPTS:
        await _set(command_id, status="error", error=reason[:500], instruction="")
        return "error"
    await _set(command_id, status="pending", error=reason[:500],
               next_attempt_at=utc_naive_now() + retry_delay(attempts))
    return "pending"


async def revive_stale() -> list[str]:
    """Вернуть зависшие в processing. → id поручений, исчерпавших попытки.

    Зависает поручение, если процесс упал посреди него. Если падает он на нём
    каждый раз, без счёта попыток оно крутилось бы вечно — поэтому исчерпавшее
    попытки уходит в error, и владельцу об этом сообщают.
    """
    now = utc_naive_now()
    stale = and_(VoiceCommandRow.status == "processing",
                 VoiceCommandRow.updated_at < now - timedelta(minutes=STALE_MINUTES))
    async with get_session() as s:
        exhausted = list((await s.execute(
            update(VoiceCommandRow)
            .where(stale, VoiceCommandRow.attempts >= MAX_ATTEMPTS)
            .values(status="error", instruction="", updated_at=now,
                    error="процесс падал на каждой попытке")
            .returning(VoiceCommandRow.command_id)
        )).scalars())
        await s.execute(
            update(VoiceCommandRow)
            .where(stale)
            .values(status="pending", updated_at=now,
                    next_attempt_at=now + retry_delay(1))
        )
    return exhausted

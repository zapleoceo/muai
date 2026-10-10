"""Дедуп и быстрый ответ для POST /v1/voice/session.

Свёртка длинной сессии идёт до ~120 с (брокер ждёт `chat:smart` столько же),
а слушатель ждёт ответа 60 с (`vera_listener.sender.TIMEOUT_S`). Раньше дедуп
стоял ПОСЛЕ свёртки: слушатель отваливался по таймауту, слал ту же сессию
снова, и каждая попытка заново гоняла свёртку — цикл без конца.

Теперь порядок такой: ключ сессии → уже есть событие? → уже обрабатывается?
→ только тогда свёртка, и ждём её не дольше `REPLY_WITHIN_S`. Не успела —
отвечаем «принято», свёртка доживает в фоне, повтор той же сессии получает
«принято» без второй обработки.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypeVar

from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow

log = logging.getLogger(__name__)
T = TypeVar("T")

#: Заметно меньше таймаута слушателя (60 с), с запасом на сеть и Cloudflare.
REPLY_WITHIN_S = 40.0

_inflight: dict[str, asyncio.Task] = {}


def voice_source_id(started_at: datetime, app: str | None, title: str | None) -> str:
    """Идентичность сессии — время начала + контекст (ключ дедупа в events)."""
    sig = f"{started_at.isoformat()}|{app}|{title}"
    return "voice:" + hashlib.sha1(sig.encode()).hexdigest()[:16]


async def find_voice_event(src_id: str) -> int | None:
    async with get_session() as s:
        return (await s.execute(
            select(EventRow.id).where(EventRow.source == "voice",
                                      EventRow.source_event_id == src_id)
        )).scalar_one_or_none()


def is_inflight(src_id: str) -> bool:
    return src_id in _inflight


async def run_once(src_id: str, work: Callable[[], Awaitable[T]]) -> T | None:
    """Выполнить `work` единожды на ключ; None — не успели, доделаем в фоне.

    Ошибка, случившаяся в пределах ожидания, пробрасывается как раньше
    (500 → сессия остаётся в очереди слушателя). Ошибка после ответа клиенту
    уже никуда не вернётся — её только логируем.
    """
    task = asyncio.create_task(work(), name=f"voice-session-{src_id}")
    _inflight[src_id] = task
    task.add_done_callback(lambda t: _finish(src_id, t))
    try:
        return await asyncio.wait_for(asyncio.shield(task), REPLY_WITHIN_S)
    except TimeoutError:
        log.warning("voice: %s дольше %.0fс — ответили «принято», свёртка в фоне",
                    src_id, REPLY_WITHIN_S)
        return None


def _finish(src_id: str, task: asyncio.Task) -> None:
    _inflight.pop(src_id, None)
    if not task.cancelled() and task.exception() is not None:
        log.error("voice: фоновая обработка %s упала: %r", src_id, task.exception())

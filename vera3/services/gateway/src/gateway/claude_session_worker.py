"""Фоновый воркер: осмысляет сессии Claude Code из очереди.

Живёт внутри шлюза и стартует в lifespan. Отдельный сервис здесь ничего бы не
дал: работа сводится к ожиданию брокера, состояние целиком в БД, а на перезапуск
контейнера воркер поднимает незакрытую сессию заново (`processing` старше
`STALE_MINUTES` возвращается в очередь).

Почему не в запросе: одно окно на 21 тыс. символов не уложилось в 120с ожидания
брокера, а сессия бывает на 20 окон — это десятки минут. Здесь ожидание брокера
поднято до `POLL_DEADLINE_S`: сам брокер бросает задание только через ~20 минут.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from vera_shared.db.engine import get_session
from vera_shared.db.models import ClaudeSessionQueueRow

from vera_shared.llm.client import LLMCoolingDown

from gateway.claude_distill import distill
from gateway.claude_session import store_summary

log = logging.getLogger(__name__)

IDLE_SLEEP_S = 30.0
#: Сколько ждать брокера на одно окно. Дефолтные 120с здесь мало: замер дал
#: 126с на окно в 21 тыс. символов, и это ещё не самый тяжёлый случай.
POLL_DEADLINE_S = 900.0
#: Три попытки, потом сессия помечается error и ждёт разбора: гонять модель по
#: кругу на ядовитой сессии дороже, чем пропустить одну.
MAX_ATTEMPTS = 3
#: Контейнер перезапустили посреди осмысления — сессия зависла в processing.
STALE_MINUTES = 45


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def revive_stale() -> int:
    """Вернуть в очередь то, что осталось в processing после перезапуска."""
    async with get_session() as s:
        result = await s.execute(
            update(ClaudeSessionQueueRow)
            .where(ClaudeSessionQueueRow.status == "processing",
                   ClaudeSessionQueueRow.updated_at
                   < _now() - timedelta(minutes=STALE_MINUTES))
            .values(status="pending", updated_at=_now())
        )
        return result.rowcount or 0


async def claim() -> ClaudeSessionQueueRow | None:
    """Взять одну сессию из очереди. Один воркер, но блокировка честная."""
    async with get_session() as s:
        chosen = (await s.execute(
            select(ClaudeSessionQueueRow.session_id)
            .where(ClaudeSessionQueueRow.status == "pending")
            .order_by(ClaudeSessionQueueRow.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if chosen is None:
            return None
        row = (await s.execute(
            update(ClaudeSessionQueueRow)
            .where(ClaudeSessionQueueRow.session_id == chosen)
            .values(status="processing", attempts=ClaudeSessionQueueRow.attempts + 1,
                    updated_at=_now())
            .returning(ClaudeSessionQueueRow)
        )).scalar_one_or_none()
        return row


async def finish(session_id: str, *, event_id: int | None, turns: int) -> None:
    """Сессия осмыслена. Сырую переписку стираем — она нужна была только для
    осмысления, а лежать на сервере ей незачем."""
    async with get_session() as s:
        await s.execute(
            update(ClaudeSessionQueueRow)
            .where(ClaudeSessionQueueRow.session_id == session_id)
            .values(status="done", done_turns=turns, event_id=event_id,
                    turns=[], error=None, updated_at=_now())
        )


async def fail(session_id: str, reason: str, attempts: int) -> None:
    status = "error" if attempts >= MAX_ATTEMPTS else "pending"
    async with get_session() as s:
        await s.execute(
            update(ClaudeSessionQueueRow)
            .where(ClaudeSessionQueueRow.session_id == session_id)
            .values(status=status, error=reason[:500], updated_at=_now())
        )
    log.warning("claude-worker: сессия %s не осмыслена (%s), попытка %d → %s",
                session_id, reason[:120], attempts, status)


async def process_one() -> bool:
    """Осмыслить одну сессию. False — очередь пуста."""
    row = await claim()
    if row is None:
        return False
    try:
        distilled, report = await distill(row.turns, project=row.project_dir,
                                         branch=row.git_branch,
                                         poll_deadline_s=POLL_DEADLINE_S)
        if not report["transcript_chars"]:
            await finish(row.session_id, event_id=None, turns=row.turn_count)
            return True
        if not report.get("distilled"):
            # Голос в этой ситуации сохраняет хотя бы факт — его звук уже
            # пропал. Здесь же сырые реплики лежат в очереди: пустышка в мозге
            # хуже, чем попробовать позже.
            await fail(row.session_id, "осмысление не удалось", row.attempts)
            return True
        event_id = await store_summary(row, distilled, report)
        await finish(row.session_id, event_id=event_id, turns=row.turn_count)
        log.info("claude-worker: сессия %s → event=%s (%d реплик, %d симв., "
                 "окон %d%s)", row.session_id, event_id, row.turn_count,
                 report["transcript_chars"], report["windows"],
                 ", ХВОСТ ОБРЕЗАН" if report["truncated"] else "")
    except LLMCoolingDown as e:
        # Предохранитель открыт (дневной бюджет, нет провайдера) — попытки не
        # жжём: виновата не сессия. False уводит цикл в сон, а не гонит его
        # мгновенными отказами по всей очереди.
        await fail(row.session_id, str(e), attempts=0)
        return False
    except Exception as e:
        # Любая причина — сессия возвращается в очередь, а не теряется.
        await fail(row.session_id, f"{type(e).__name__}: {e}", row.attempts)
    return True


async def run_forever() -> None:
    log.info("claude-worker: запущен")
    revived = await revive_stale()
    if revived:
        log.info("claude-worker: вернул в очередь после перезапуска: %d", revived)
    while True:
        try:
            busy = await process_one()
        except asyncio.CancelledError:
            log.info("claude-worker: остановлен")
            raise
        except Exception:
            # Сбой БД не имеет права убить воркер: иначе очередь встанет молча.
            log.exception("claude-worker: цикл упал")
            busy = False
        if not busy:
            await asyncio.sleep(IDLE_SLEEP_S)

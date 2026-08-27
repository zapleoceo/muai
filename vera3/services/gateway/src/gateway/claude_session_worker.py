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

from sqlalchemy import case, select, update
from vera_shared.db.engine import get_session
from vera_shared.db.models import ClaudeSessionQueueRow
from vera_shared.llm.circuit import llm_cooldown_remaining_s
from vera_shared.llm.client import LLMCoolingDown

from gateway.claude_distill import SPEC, distill
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
#: Потолок на один сон по кулдауну: кап брокера сбрасывается в 00:00 UTC, и
#: ждать до него одним куском значит проспать досрочно ожившый пул.
MAX_COOLDOWN_SLEEP_S = 600.0


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
        return (await s.execute(
            update(ClaudeSessionQueueRow)
            .where(ClaudeSessionQueueRow.session_id == chosen)
            .values(status="processing", attempts=ClaudeSessionQueueRow.attempts + 1,
                    updated_at=_now())
            .returning(ClaudeSessionQueueRow)
        )).scalar_one_or_none()


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


async def defer(session_id: str, reason: str) -> None:
    """Вернуть сессию в очередь, не засчитывая попытку.

    Предохранитель открыт — виновата не сессия, и счётчик попыток обязан
    остаться честным: иначе она израсходует все три, ни разу не попробовав, и
    первый же настоящий сбой пометит её error.
    """
    async with get_session() as s:
        await s.execute(
            update(ClaudeSessionQueueRow)
            .where(ClaudeSessionQueueRow.session_id == session_id)
            .values(status="pending", error=reason[:500], updated_at=_now(),
                    # CASE, а не greatest(): в SQLite такой функции нет, а
                    # тесты очереди гоняются на живом SQLite.
                    attempts=case(
                        (ClaudeSessionQueueRow.attempts > 0,
                         ClaudeSessionQueueRow.attempts - 1),
                        else_=0))
        )


async def cooling_s() -> float:
    """Секунд до конца кулдауна пулов, которыми осмысляем. 0 — можно работать.

    Проверяем ДО claim: иначе воркер каждые 30с забирает сессию, получает отказ
    предохранителя и кладёт обратно. Поймано вживую — за один кулдаун одна
    сессия набрала 122 попытки, и первый же настоящий сбой после этого сразу
    пометил бы её error, хотя своих попыток у неё не было ни одной.
    """
    waits = [await llm_cooldown_remaining_s(cap)
             for cap in {SPEC.part_capability, SPEC.merge_capability}]
    return max(waits)


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
        # Предохранитель открылся уже после claim (проверка перед ним есть, но
        # окно между ними существует). Попытку не засчитываем, а False уводит
        # цикл в сон, а не гонит его отказами по всей очереди.
        await defer(row.session_id, str(e))
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
            cooling = await cooling_s()
            if cooling > 0:
                log.info("claude-worker: предохранитель закрыт, жду %.0f мин",
                         cooling / 60)
                await asyncio.sleep(min(cooling + 5, MAX_COOLDOWN_SLEEP_S))
                continue
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

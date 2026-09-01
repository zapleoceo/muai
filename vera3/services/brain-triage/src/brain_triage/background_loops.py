"""Long-running background loops started once from main_loop():
watchdog (recover stuck 'processing' events) and retry-with-backoff
(recover 'error' events until MAX_RETRIES, then 'dead')."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from vera_shared.db.engine import get_session

from brain_triage.config import (
    REL_EXTRACT_CONCURRENCY,
    REL_EXTRACT_TIMEOUT_S,
    STUCK_AFTER_S,
)

log = logging.getLogger(__name__)

# Держим ссылки на фоновые rel-extract задачи, иначе GC может их выбросить.
_bg_tasks: set[asyncio.Task] = set()


def track(task: asyncio.Task) -> asyncio.Task:
    """Взять фоновую задачу под ссылку и снять её по завершении.

    Событийный цикл держит на задачу только СЛАБУЮ ссылку — без сильной её
    может собрать GC на полпути. Приём применялся в пяти местах копипастой;
    здесь он один, чтобы следующий вызов create_task не забыл про него.
    """
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def start_background_loops() -> list[asyncio.Task]:
    """Поднять watchdog и retry-цикл ПОД ССЫЛКАМИ. Зовётся из main_loop().

    Раньше обе задачи создавались голым `asyncio.create_task(...)` с
    отброшенным результатом — единственные два таких места во всём vera3
    (ruff RUF006), и притом самые важные: если watchdog исчезнет, события,
    застрявшие в `processing`, не вернутся в `pending` никогда — другого
    механизма восстановления нет, а монитор считает только очередь
    `pending` и такой сбой скорее занизит видимый бэклог, чем покажет его.
    """
    return [track(asyncio.create_task(_watchdog_loop(), name="triage-watchdog")),
            track(asyncio.create_task(_retry_failed_loop(), name="triage-retry"))]


# Потолок одновременных rel-extract на процесс. Создаётся лениво: семафор
# привязывается к событийному циклу, а модуль импортируется до его старта.
# ВАЖНО: он модульный, а не пересоздаваемый на цикл, как sem в
# process_pending() — тот ограничивает только передний план и только внутри
# одного вызова, поэтому фоновые задачи накапливались МЕЖДУ вызовами.
_rel_sem: asyncio.Semaphore | None = None


def _rel_semaphore() -> asyncio.Semaphore:
    global _rel_sem
    if _rel_sem is None:
        _rel_sem = asyncio.Semaphore(REL_EXTRACT_CONCURRENCY)
    return _rel_sem


async def _safe_rel_extract(event_id: int, body: str) -> None:
    """Rel extraction в фоне; никогда не роняет триаж, но сбой виден в логах.

    Под семафором и с таймаутом. Без них число одновременных задач упиралось
    не во что: process_pending крутится каждые ~1-3 с при наличии работы, а
    одна задача живёт до брокерского потолка в 120 с — за это время успевает
    накопиться несколько десятков, каждая со своим походом в пул на 10
    соединений, общий с claim'ом и записью статусов.
    """
    try:
        from vera_shared.graph.rel_extract import extract_and_store
        async with _rel_semaphore():
            await asyncio.wait_for(extract_and_store(event_id, body),
                                   timeout=REL_EXTRACT_TIMEOUT_S)
    except TimeoutError:
        log.warning("rel_extract event=%s: таймаут %.0fс (граф не построен)",
                    event_id, REL_EXTRACT_TIMEOUT_S)
    except Exception as e:
        log.warning("rel_extract event=%s failed (граф не построен): %s", event_id, e)


async def _watchdog_loop() -> None:
    """Возвращает 'processing' события в 'pending' если воркер крашнулся.

    Использует `triage_started_at` (когда захвачено), НЕ `received_at`.
    Это исправляет баг: старое pending событие (received_at месячной давности)
    мгновенно реверится сразу после claim'a.
    """
    sql = (
        "UPDATE events SET "
        "  triage_status='pending', "
        "  triage_started_at=NULL "
        "WHERE triage_status='processing' "
        f"  AND triage_started_at < NOW() - INTERVAL '{STUCK_AFTER_S} seconds' "
        "RETURNING id"
    )
    while True:
        await asyncio.sleep(60)
        try:
            async with get_session() as s:
                rs = await s.execute(text(sql))
                stuck = list(rs.scalars().all())
            if stuck:
                log.warning("Watchdog: %d stuck events returned to pending: %s",
                            len(stuck), stuck[:5])
        except Exception as e:
            log.warning("Watchdog error: %s", e)


BACKOFF_MINUTES = [1, 5, 30, 120, 720]   # 1m, 5m, 30m, 2h, 12h → then dead
MAX_RETRIES = len(BACKOFF_MINUTES)


async def _retry_failed_loop() -> None:
    """Pick up 'error' events whose backoff window expired, re-pend them.

    Counter prevents flapping: each retry pushes next attempt further out.
    After MAX_RETRIES attempts, status='dead' — drops out of the loop and
    becomes visible in the dashboard as 'truly stuck, needs manual review'.
    """
    # Two-phase, чтобы КАЖДАЯ попытка ждала свой backoff (старый одношаговый
    # UPDATE ре-пендил первую ошибку мгновенно и брал [rc+2] — 1m не
    # использовался, счёт съезжал на один).
    # Phase 1 (schedule): свежий 'error' получает next_retry_at = NOW() +
    #   BACKOFF[rc] (rc уже сделанных попыток; SQL-массив 1-индексный → [rc+1]);
    #   исчерпал MAX_RETRIES — сразу 'dead'.
    # Phase 2 (release): дозревшие по next_retry_at → 'pending', rc+1.
    schedule_sql = text("""
        UPDATE events SET
          triage_status = CASE
            WHEN triage_retry_count >= :max_retries THEN 'dead'
            ELSE triage_status
          END,
          triage_next_retry_at = CASE
            WHEN triage_retry_count >= :max_retries THEN NULL
            ELSE NOW() + (
              (CAST(:backoff AS int[]))[triage_retry_count + 1]
              || ' minutes'
            )::interval
          END
        WHERE triage_status = 'error'
          AND triage_next_retry_at IS NULL
        RETURNING id, triage_status
    """)
    release_sql = text("""
        UPDATE events SET
          triage_status = 'pending',
          triage_retry_count = triage_retry_count + 1,
          triage_started_at = NULL,
          triage_next_retry_at = NULL
        WHERE triage_status = 'error'
          AND triage_next_retry_at IS NOT NULL
          AND triage_next_retry_at < NOW()
        RETURNING id, triage_retry_count
    """)
    while True:
        await asyncio.sleep(120)
        try:
            async with get_session() as s:
                scheduled = list((await s.execute(
                    schedule_sql,
                    {"max_retries": MAX_RETRIES, "backoff": BACKOFF_MINUTES},
                )).mappings().all())
                released = list((await s.execute(release_sql)).mappings().all())
            dead = [r for r in scheduled if r["triage_status"] == "dead"]
            if scheduled or released:
                log.info("retry-loop: scheduled %d, re-pended %d, dead %d",
                         len(scheduled) - len(dead), len(released), len(dead))
        except Exception as e:
            log.warning("retry-loop error: %s", e)

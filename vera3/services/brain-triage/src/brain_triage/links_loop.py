"""Построение `event_entities` по новым событиям (миграция 042).

Раз в LINKS_INTERVAL_S берёт не больше LINKS_MAX_BATCHES пачек новых событий после курсора
`forward` и пересобирает их связи (автор, получатель, участники созвона, упомянутые).
Старые события догоняет `scripts/backfill_event_links.py`. Реплик brain-triage может быть
несколько: цикл идёт под advisory-замком, вторая реплика пропускает проход.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from vera_shared.db.engine import get_engine
from vera_shared.links.context import ContextBuilder
from vera_shared.links.index import FORWARD, Resources, load_resources, run_batch
from vera_shared.links.index_store import next_batch

from brain_triage.config import (
    LINKS_BATCH,
    LINKS_INTERVAL_S,
    LINKS_MAX_BATCHES,
    LINKS_START_DELAY_S,
)

log = logging.getLogger(__name__)

LINKS_LOCK_KEY = 7_340_042
#: Один построитель на процесс: круги чатов считаются раз в час, а не на каждый проход.
_builder = ContextBuilder()
RESOURCES_TTL_S = 600.0
_res: Resources | None = None
_res_at = 0.0


@asynccontextmanager
async def _cycle_lock() -> AsyncIterator[bool]:
    """Замок уровня сессии на отдельном соединении в AUTOCOMMIT: открытой транзакции на время
    прохода нет (idle-in-transaction держал бы снимок и мешал вакууму). На не-Postgres
    (тесты) замка нет — проход свободен."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield True
        return
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                       {"k": LINKS_LOCK_KEY})).scalar_one())
        try:
            yield got
        finally:
            if got:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LINKS_LOCK_KEY})


async def _resources() -> Resources:
    """Люди и прозвища меняются редко: пересобираем не чаще раза в RESOURCES_TTL_S."""
    global _res, _res_at
    if _res is None or time.monotonic() - _res_at > RESOURCES_TTL_S:
        await _builder.preload()
        _res, _res_at = await load_resources(_builder.owner), time.monotonic()
    return _res


async def run_links_cycle() -> int:
    """Один проход; → число обработанных событий (-1 — замок у другой реплики). Нет новых
    событий — выход до замка и до загрузки ресурсов (один дешёвый запрос)."""
    if not await next_batch(FORWARD, 1):
        return 0
    async with _cycle_lock() as got:
        if not got:
            return -1
        res = await _resources()
        total = 0
        for _ in range(LINKS_MAX_BATCHES):
            result = await run_batch(FORWARD, res, _builder, LINKS_BATCH)
            if result.last_id is None:
                break
            total += result.events
            if result.skipped:
                log.warning("links: пропущено событий: %s", list(result.skipped))
        return total


async def links_loop() -> None:
    await asyncio.sleep(LINKS_START_DELAY_S)
    while True:
        try:
            done = await run_links_cycle()
        except Exception:
            log.exception("links: проход не удался (миграция 042 накачена?)")
        else:
            if done > 0:
                log.info("links: связи построены для %d событий", done)
        await asyncio.sleep(LINKS_INTERVAL_S)

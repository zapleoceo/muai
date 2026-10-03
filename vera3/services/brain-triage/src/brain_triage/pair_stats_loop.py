"""Периодическая пересборка `pair_stats` (миграция 040).

Кэш взаимодействий пар нужен карточке, графу и чистке связей; считается целиком
одним INSERT … SELECT на стороне Postgres. Первый проход — через
PAIR_STATS_START_DELAY_S после старта, чтобы не толкаться с запуском реплик.
"""
from __future__ import annotations

import asyncio
import logging

from vera_shared.graph.pair_stats import refresh_pair_stats

from brain_triage.config import PAIR_STATS_INTERVAL_S, PAIR_STATS_START_DELAY_S

log = logging.getLogger(__name__)


async def pair_stats_loop() -> None:
    await asyncio.sleep(PAIR_STATS_START_DELAY_S)
    while True:
        try:
            pairs = await refresh_pair_stats()
        except Exception:
            log.exception("pair-stats: пересборка не удалась (миграция 040 накачена?)")
        else:
            if pairs is None:
                log.info("pair-stats: пересборку уже ведёт другая реплика")
            else:
                log.info("pair-stats: пересобрано %d пар", pairs)
        await asyncio.sleep(PAIR_STATS_INTERVAL_S)

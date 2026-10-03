#!/usr/bin/env python
"""Пересобрать pair_stats вручную (первое заполнение после миграции 040).

    docker compose run --rm --no-deps brain-triage python /scripts/refresh_pair_stats.py

Тот же проход, что делает `pair_stats_loop` раз в шесть часов; безопасно
повторять. Нужны DATABASE_URL и OWNER_TELEGRAM_ID.
"""
from __future__ import annotations

import asyncio
import sys

from vera_shared.db.engine import close_engine, init_engine
from vera_shared.graph.pair_stats import refresh_pair_stats


async def main() -> int:
    await init_engine()
    try:
        pairs = await refresh_pair_stats()
    finally:
        await close_engine()
    if pairs is None:
        print("пересборку уже ведёт другая реплика", file=sys.stderr)
        return 1
    print(f"pair_stats: {pairs} пар")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

#!/usr/bin/env python
"""Догнать `event_entities` по старым событиям: резюмируемо, пачками, ночью.

    docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts brain-triage \
        python /scripts/backfill_event_links.py --batch 500 --max-batches 200

Идёт от больших id к меньшим (свежее важнее), курсор `backfill` в `link_cursor` —
остановка и повтор безопасны. `--reset` начинает заново (после смены прозвищ, карты
голосов или правил); `--status` печатает курсоры и ничего не меняет. Пачка пишется
одной транзакцией; между пачками пауза `--pause`, чтобы не мешать триажу. Нужны
DATABASE_URL и OWNER_TELEGRAM_ID.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from sqlalchemy import select
from vera_shared.db.engine import close_engine, get_session, init_engine
from vera_shared.db.models_links import LinkCursorRow
from vera_shared.links.context import ContextBuilder
from vera_shared.links.index import (
    BACKFILL,
    DEFAULT_BATCH,
    FORWARD,
    load_resources,
    max_event_id,
    reset_cursors,
    run_batch,
)


async def _cursors() -> dict[str, int]:
    async with get_session() as s:
        rows = (await s.execute(select(LinkCursorRow))).scalars().all()
    return {r.name: r.event_id for r in rows}


async def main(args: argparse.Namespace) -> int:
    await init_engine()
    try:
        if args.status:
            print({"max_event_id": await max_event_id(), **await _cursors()})
            return 0
        if args.reset:
            print(f"курсоры сброшены, max(id)={await reset_cursors()}")
        builder = ContextBuilder()
        await builder.build([])
        res = await load_resources(builder.owner)
        started, events, links = time.monotonic(), 0, 0
        for number in range(1, args.max_batches + 1):
            result = await run_batch(BACKFILL, res, builder, args.batch)
            if result.last_id is None:
                print("backfill закончен")
                break
            events, links = events + result.events, links + result.links
            print(f"пачка {number}: до id {result.last_id}, событий {events}, связей {links}, "
                  f"{time.monotonic() - started:.0f} с", flush=True)
            await asyncio.sleep(args.pause)
        await run_batch(FORWARD, res, builder, args.batch)
    finally:
        await close_engine()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    p.add_argument("--max-batches", type=int, default=100)
    p.add_argument("--pause", type=float, default=0.5)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--status", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))

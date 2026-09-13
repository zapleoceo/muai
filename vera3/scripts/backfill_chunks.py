"""Куски для уже существующих длинных событий (миграция 032).

    python scripts/backfill_chunks.py --estimate           # сколько событий/кусков/токенов
    python scripts/backfill_chunks.py [--batch 20] [--limit 500]

Запускать в контейнере триажа (там brain_triage и брокер):
    docker exec -i vera3-brain-triage-1 python - --estimate < scripts/backfill_chunks.py

Хвосты, отрезанные на входе до 2026-09-13 (потолок 8000), не вернёт ничто —
их нет в базе. Скрипт режет то, что есть: события длиннее CHUNK_THRESHOLD, с
вектором события и без кусков. Замер прода 2026-09-13: 1741 событие, ~9.8 тыс.
кусков, ~15 млн символов → порядка 5 млн токенов voyage-4. Перезапуск
безопасен: события с кусками пропускаются, идём по id.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from brain_triage.chunks import embed_event_chunks
from brain_triage.postprocess import SKIP_EMBED_SOURCES
from sqlalchemy import text
from vera_shared.db.chunk_vectors import CHUNK_TABLE, chunk_table_available
from vera_shared.db.engine import get_session, init_engine
from vera_shared.text_chunks import CHUNK_THRESHOLD, split_chunks

log = logging.getLogger("backfill-chunks")

#: Грубая оценка для смеси русского и английского у voyage-4.
CHARS_PER_TOKEN = 3.0

_PENDING = f"""
    SELECT e.id, e.content_text FROM events e
    JOIN event_embeddings ee ON ee.event_id = e.id
    WHERE e.id > :after AND length(e.content_text) > :threshold
      AND e.source <> ALL(:skip)
      AND NOT EXISTS (SELECT 1 FROM {CHUNK_TABLE} c WHERE c.event_id = e.id)
    ORDER BY e.id LIMIT :n
"""


async def fetch_pending(after: int, n: int) -> list[tuple[int, str]]:
    async with get_session() as s:
        rows = (await s.execute(text(_PENDING), {
            "after": after, "threshold": CHUNK_THRESHOLD, "n": n,
            "skip": sorted(SKIP_EMBED_SOURCES)})).all()
    return [(r[0], r[1]) for r in rows]


def estimate(events: list[tuple[int, str]]) -> dict[str, int]:
    pieces = [p for _, body in events for p in split_chunks(body)]
    chars = sum(len(p) for p in pieces)
    return {"events": len(events), "chunks": len(pieces), "chars": chars,
            "tokens": int(chars / CHARS_PER_TOKEN)}


async def run(batch: int, limit: int, only_estimate: bool) -> dict[str, int]:
    await init_engine()
    if not await chunk_table_available():
        raise SystemExit("таблицы event_chunk_embeddings нет — сначала миграция 032")
    after, done, seen = 0, 0, []
    while done < limit:
        events = await fetch_pending(after, min(batch, limit - done))
        if not events:
            break
        after = events[-1][0]
        done += len(events)
        if only_estimate:
            seen.extend(events)
            continue
        stored = await embed_event_chunks(events)
        log.info("до id=%d: %d событий, с кусками %d", after, len(events), stored)
    return estimate(seen) if only_estimate else {"events": done}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--limit", type=int, default=100_000)
    args = ap.parse_args()
    print(asyncio.run(run(args.batch, args.limit, args.estimate)))


if __name__ == "__main__":
    main()

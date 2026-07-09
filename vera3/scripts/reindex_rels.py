"""One-off: back-repair графа — прогнать rel-extract по важным событиям
(importance>=THRESHOLD), у которых нет ни одной связи. Идемпотентно (upsert).

Троттлинг: низкая конкуренция + backoff на сбоях брокера (503), чтобы не
задавить общий брокер, которым пользуется живой триаж. Keyset-пагинация по id.

Запуск (transient-контейнер с env/сетью брокера):
  docker compose run -d --no-deps --rm \
    -v /var/www/vera3/scripts:/scripts brain-triage \
    python /scripts/reindex_rels.py
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text
from vera_shared.db.engine import get_session, init_engine
from vera_shared.graph.rel_extract import extract_and_store
from vera_shared.llm.broker_client import BrokerCallFailed

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s reindex: %(message)s")
log = logging.getLogger("reindex")

THRESHOLD = int(os.environ.get("REINDEX_IMPORTANCE", "70"))
CONCURRENCY = int(os.environ.get("REINDEX_CONCURRENCY", "2"))
PAGE = 500
MAX_BACKOFF_S = 120.0


async def _fetch_page(after_id: int) -> list[tuple[int, str]]:
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT e.id, e.content_text
            FROM events e
            WHERE e.triage_status = 'done'
              AND COALESCE(e.importance, 0) >= :thr
              AND e.content_text <> ''
              AND e.id > :after
              AND NOT EXISTS (
                SELECT 1 FROM relationships r WHERE r.derived_from_event_id = e.id
              )
            ORDER BY e.id
            LIMIT :page
        """), {"thr": THRESHOLD, "after": after_id, "page": PAGE})).all()
    return [(r[0], r[1]) for r in rows]


async def _process_one(sem: asyncio.Semaphore, eid: int, body: str, stats: dict) -> None:
    async with sem:
        backoff = 2.0
        for attempt in range(5):
            try:
                await extract_and_store(eid, body)
                stats["ok"] += 1
                return
            except BrokerCallFailed as e:
                # Брокер перегружен/503 — ждём и повторяем, не бросаем нагрузку.
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                if attempt == 4:
                    stats["skipped"] += 1
                    log.warning("event=%s пропущено после ретраев: %s", eid, e)
            except Exception as e:  # noqa: BLE001
                stats["skipped"] += 1
                log.warning("event=%s ошибка: %s", eid, e)
                return


async def main() -> None:
    await init_engine()
    log.info("reindex rel-extract start: importance>=%d, concurrency=%d",
             THRESHOLD, CONCURRENCY)
    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {"ok": 0, "skipped": 0}
    after_id = 0
    total = 0
    while True:
        page = await _fetch_page(after_id)
        if not page:
            break
        after_id = page[-1][0]
        await asyncio.gather(*(_process_one(sem, eid, body, stats)
                              for eid, body in page))
        total += len(page)
        log.info("progress: %d обработано (ok=%d skipped=%d), last_id=%d",
                 total, stats["ok"], stats["skipped"], after_id)
    log.info("reindex DONE: total=%d ok=%d skipped=%d",
             total, stats["ok"], stats["skipped"])


if __name__ == "__main__":
    asyncio.run(main())

"""One-off: re-embed every event_embeddings row after the broker's default voyage model
changed (voyage-3 -> voyage-4, 2026-07-07). Same 1024 dims, but a DIFFERENT vector space --
old voyage-3 rows would silently score wrong against new voyage-4 query vectors (brain-search's
_cosine only guards against differing LENGTH, not differing space, and voyage-4 also outputs
1024 dims).

Walks events in id order, embeds content_text in batches, upserts the SAME row (matches the
INSERT ... ON CONFLICT pattern brain-triage's worker already uses) so no schema change and no
downtime — search just keeps reading whatever's in the table, freshest-embedded first behind.

Run in the container:
    docker exec -w /app <brain-triage-container> python scripts/reembed_voyage4.py [--start-id N]

Resumable: pass --start-id to continue after a crash/restart (logs the last processed id
every batch).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

from sqlalchemy import text
from vera_shared.db.engine import get_session, init_engine
from vera_shared.llm.client import embed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reembed-voyage4")

BATCH_SIZE = 32
MAX_CHARS = 8000  # matches triage_one's content_text truncation


async def _fetch_batch(after_id: int) -> list[tuple[int, str]]:
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT e.id, e.content_text
            FROM events e
            JOIN event_embeddings ee ON ee.event_id = e.id
            WHERE e.id > :after AND e.content_text != ''
            ORDER BY e.id
            LIMIT :limit
        """), {"after": after_id, "limit": BATCH_SIZE})).all()
    return [(r[0], r[1]) for r in rows]


async def _upsert(event_id: int, vector: list[float]) -> None:
    async with get_session() as s:
        await s.execute(text("""
            INSERT INTO event_embeddings (event_id, embedding)
            VALUES (:eid, CAST(:emb AS jsonb))
            ON CONFLICT (event_id) DO UPDATE SET embedding = EXCLUDED.embedding
        """), {"eid": event_id, "emb": json.dumps(vector)})


async def main(start_id: int) -> None:
    await init_engine()
    after_id = start_id
    total = 0
    while True:
        batch = await _fetch_batch(after_id)
        if not batch:
            break
        texts = [(content or "")[:MAX_CHARS] for _, content in batch]
        try:
            vectors = await embed(texts)
        except Exception as e:
            log.warning("batch after id=%d failed, retrying one-by-one: %s", after_id, e)
            vectors = []
            for t in texts:
                try:
                    vectors.append((await embed([t]))[0])
                except Exception as e2:
                    log.warning("single embed failed for a row, skipping: %s", e2)
                    vectors.append(None)
        for (event_id, _), vec in zip(batch, vectors, strict=True):
            if vec is not None:
                await _upsert(event_id, vec)
        after_id = batch[-1][0]
        total += len(batch)
        if total % (BATCH_SIZE * 20) == 0:
            log.info("progress: %d rows re-embedded, last id=%d", total, after_id)
    log.info("done: %d rows re-embedded, last id=%d", total, after_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-id", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(main(args.start_id))

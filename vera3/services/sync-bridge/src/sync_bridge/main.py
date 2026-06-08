"""Sync bridge: тянет новые события из Vera 2.0 SQLite → Vera 3.0 Postgres.

Запускается раз в минуту. Идемпотентно (dedup по source + source_event_id).
Удалится когда напишем ingestor'ы и переключимся полностью на Vera 3.0 capture.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow

log = logging.getLogger("sync_bridge")

VERA2_SQLITE = Path(os.environ.get("VERA2_SQLITE", "/vera2/vera.db"))
POLL_S = int(os.environ.get("SYNC_POLL_S", "60"))


def _parse(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def _parse_dt(raw):
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(str(raw).split(".")[0])
    except Exception:
        return datetime.utcnow()


async def sync_tick() -> int:
    """Fetch новые события из SQLite (с момента max(received_at) в Postgres)."""
    async with get_session() as s:
        max_received = (await s.execute(
            select(EventRow.received_at).order_by(EventRow.received_at.desc()).limit(1)
        )).scalar()

    cutoff = max_received or datetime(2020, 1, 1)

    if not VERA2_SQLITE.exists():
        log.warning("vera2 sqlite not found: %s", VERA2_SQLITE)
        return 0

    conn = sqlite3.connect(VERA2_SQLITE)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT id AS v2_id, source, source_event_id, account, category, content_text, "
        "content_extra, entity_hints, metadata, occurred_at, received_at, "
        "graphiti_episode_uuid "
        "FROM events WHERE received_at > ? ORDER BY received_at"
    )
    rows = list(conn.execute(sql, (cutoff.isoformat(),)))
    conn.close()

    if not rows:
        return 0

    # Получим существующие keys для dedup
    keys_to_check = [(r["source"], r["source_event_id"] or f"v2migrated:{r['v2_id']}")
                     for r in rows]
    async with get_session() as s:
        for batch_start in range(0, len(keys_to_check), 500):
            batch = keys_to_check[batch_start:batch_start + 500]
            srcs = list({k[0] for k in batch})
            sids = list({k[1] for k in batch})
            existing_rs = await s.execute(
                select(EventRow.source, EventRow.source_event_id)
                .where(EventRow.source.in_(srcs))
                .where(EventRow.source_event_id.in_(sids))
            )
            existing = {(r.source, r.source_event_id) for r in existing_rs}

            for r in rows[batch_start:batch_start + 500]:
                sid = r["source_event_id"] or f"v2migrated:{r['v2_id']}"
                if (r["source"], sid) in existing:
                    continue
                row = EventRow(
                    source=r["source"],
                    source_event_id=sid,
                    account=r["account"],
                    category=r["category"] or "generic",
                    content_text=r["content_text"] or "",
                    content_extra=_parse(r["content_extra"]),
                    entity_hints=_parse(r["entity_hints"]) or [],
                    metadata_=_parse(r["metadata"]),
                    occurred_at=_parse_dt(r["occurred_at"]),
                    graphiti_episode_uuid=r["graphiti_episode_uuid"],
                    triage_status="pending",
                )
                s.add(row)

    return len(rows)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("sync-bridge started, polling %s every %ss", VERA2_SQLITE, POLL_S)
    while True:
        try:
            n = await sync_tick()
            if n:
                log.info("Synced %s events", n)
        except Exception as e:
            log.exception("Sync error: %s", e)
        await asyncio.sleep(POLL_S)


if __name__ == "__main__":
    asyncio.run(main())

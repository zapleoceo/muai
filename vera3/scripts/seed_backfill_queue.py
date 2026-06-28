#!/usr/bin/env python
"""Seed backfill_jobs queue.

Usage:
  docker exec vera3-ingestor-telegram python -m scripts.seed_backfill_queue 2025-06-01

Populates one job per dialog (DM + groups + channels), all targeting the
given floor date. The userbot's backfill_worker loop then walks each one
backwards in pages until it reaches floor or runs out of history.

ON CONFLICT DO NOTHING — safe to re-run; existing jobs untouched.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime

from sqlalchemy import text
from telethon import TelegramClient
from telethon.sessions import StringSession

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models_sources import TelegramSessionRow
from vera_shared.tokens.crypto import decrypt
from sqlalchemy import select

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed-backfill")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ["TELEGRAM_PHONE"]


async def _load_session_string() -> str:
    async with get_session() as s:
        row = (await s.execute(
            select(TelegramSessionRow).where(
                TelegramSessionRow.phone == PHONE,
                TelegramSessionRow.is_active.is_(True),
            )
        )).scalar_one_or_none()
    if not row:
        raise RuntimeError(f"No active session for phone={PHONE}")
    return decrypt(row.session_string_enc)


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: seed_backfill_queue.py YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    floor = datetime.strptime(sys.argv[1], "%Y-%m-%d")
    log.info("Seeding backfill jobs targeting floor=%s", floor)

    await init_engine()
    sess = await _load_session_string()
    client = TelegramClient(StringSession(sess), API_ID, API_HASH)
    await client.connect()

    inserted = 0
    skipped = 0

    async for dialog in client.iter_dialogs():
        chat_title = (getattr(dialog.entity, "title", None)
                      or getattr(dialog.entity, "first_name", None)
                      or "(private)")
        chat_id = dialog.id

        async with get_session() as s:
            r = await s.execute(text("""
                INSERT INTO backfill_jobs (chat_id, chat_title, target_floor_date)
                VALUES (:chat_id, :chat_title, :floor)
                ON CONFLICT (chat_id, target_floor_date) DO NOTHING
                RETURNING id
            """), {"chat_id": chat_id, "chat_title": chat_title, "floor": floor})
            new_id = r.scalar_one_or_none()

        if new_id:
            inserted += 1
            log.info("  + job %s for chat %s '%s'", new_id, chat_id, chat_title)
        else:
            skipped += 1

    log.info("Done: inserted=%s skipped=%s (already queued)", inserted, skipped)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

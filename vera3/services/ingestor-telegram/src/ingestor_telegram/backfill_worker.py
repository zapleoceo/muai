"""Persistent backfill — walks every dialog back to TARGET_FLOOR_DATE.

Loop:
  1. SELECT one job WHERE status IN ('pending','in_progress')
     ORDER BY status DESC (in_progress first → resume), created_at
  2. Walk that dialog in pages of 100 messages, going backwards from
     cursor_msg_id (or newest if NULL).
  3. After each page: insert events + update cursor_msg_id +
     cursor_oldest_date. If oldest_date <= target_floor_date → status=completed.
  4. FloodWaitError → sleep(seconds) and resume same job next iteration.
  5. Move to next job only when current is completed/error.

Use:
  scripts/seed_backfill_queue.py 2025-06-01    # one-time, populates jobs
  Worker runs forever inside the userbot container.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import text
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from sqlalchemy import select as sa_select

log = logging.getLogger("tg-backfill-queue")

PAGE_SIZE = int(os.environ.get("BACKFILL_PAGE_SIZE", "100"))
IDLE_S = int(os.environ.get("BACKFILL_IDLE_S", "60"))


async def _claim_next_job() -> dict | None:
    """Pick next job: prefer resume over fresh start."""
    async with get_session() as s:
        rs = await s.execute(text("""
            UPDATE backfill_jobs SET
              status = 'in_progress',
              started_at = COALESCE(started_at, NOW())
            WHERE id = (
              SELECT id FROM backfill_jobs
              WHERE status IN ('pending', 'in_progress')
              ORDER BY (status = 'in_progress') DESC, created_at
              LIMIT 1
              FOR UPDATE SKIP LOCKED
            )
            RETURNING id, chat_id, chat_title, target_floor_date,
                      cursor_msg_id, cursor_oldest_date, pages_done,
                      messages_inserted
        """))
        m = rs.mappings().first()
        return dict(m) if m else None


async def _mark_job(job_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    fields["job_id"] = job_id
    async with get_session() as s:
        await s.execute(text(f"UPDATE backfill_jobs SET {cols} WHERE id = :job_id"),
                        fields)


async def _process_page(client: TelegramClient, job: dict, me_id: int) -> dict:
    """One page (PAGE_SIZE messages backwards). Returns delta dict."""
    from ingestor_telegram.userbot import save_message   # reuse the saver

    chat = await client.get_entity(int(job["chat_id"]))
    offset_id = int(job["cursor_msg_id"] or 0)
    inserted = 0
    oldest_seen: datetime | None = None
    floor: datetime = job["target_floor_date"]

    msgs = await client.get_messages(chat, limit=PAGE_SIZE, offset_id=offset_id)
    if not msgs:
        return {"done": True, "reason": "no messages (start of history)",
                "inserted": 0, "oldest_seen": None}

    for msg in msgs:
        if msg.date is None:
            continue
        msg_date = msg.date.replace(tzinfo=None)
        if oldest_seen is None or msg_date < oldest_seen:
            oldest_seen = msg_date

        # Skip if older than floor — page might span the boundary
        if msg_date < floor:
            continue

        async with get_session() as s:
            exists = (await s.execute(
                sa_select(EventRow.id).where(
                    EventRow.source == "telegram",
                    EventRow.source_event_id == f"tg:{chat.id}:{msg.id}",
                )
            )).scalar_one_or_none()
        if exists:
            continue

        # Reuse the userbot's save_message — same Author/media/etc contract
        try:
            await save_message(client, msg)
            inserted += 1
        except Exception as e:
            log.warning("save_message failed (msg=%s): %s", msg.id, e)

    new_cursor = msgs[-1].id if msgs else offset_id
    return {
        "done": oldest_seen is not None and oldest_seen <= floor,
        "reason": "reached floor" if oldest_seen and oldest_seen <= floor else "more to go",
        "inserted": inserted,
        "oldest_seen": oldest_seen,
        "new_cursor": new_cursor,
    }


async def backfill_loop(client: TelegramClient) -> None:
    """Run forever. One job → walk to floor → next job."""
    me = await client.get_me()
    log.info("backfill-queue worker started")

    while True:
        job = await _claim_next_job()
        if not job:
            await asyncio.sleep(IDLE_S)
            continue

        log.info("backfill: job=%s chat=%s '%s' cursor=%s pages_done=%s",
                 job["id"], job["chat_id"], job["chat_title"],
                 job["cursor_msg_id"], job["pages_done"])

        # Walk pages until done / error
        while True:
            try:
                result = await _process_page(client, job, me.id)
            except FloodWaitError as e:
                wait = min(int(e.seconds) + 5, 600)
                log.warning("FloodWait %ss on job %s — sleeping",
                            wait, job["id"])
                await _mark_job(job["id"], status="pending",
                                last_error=f"flood-wait {wait}s")
                await asyncio.sleep(wait)
                break    # re-claim next iteration
            except Exception as e:
                log.exception("job %s page failed: %s", job["id"], e)
                await _mark_job(job["id"], status="error",
                                last_error=f"{type(e).__name__}: {e}"[:500],
                                finished_at=datetime.utcnow())
                break

            new_msg_count = job["messages_inserted"] + result["inserted"]
            new_pages = job["pages_done"] + 1

            if result["done"]:
                await _mark_job(
                    job["id"], status="completed",
                    cursor_msg_id=result.get("new_cursor"),
                    cursor_oldest_date=result["oldest_seen"],
                    pages_done=new_pages,
                    messages_inserted=new_msg_count,
                    finished_at=datetime.utcnow(),
                    last_error=result["reason"],
                )
                log.info("backfill: job=%s COMPLETED — %s msgs over %s pages",
                         job["id"], new_msg_count, new_pages)
                break

            await _mark_job(
                job["id"],
                cursor_msg_id=result.get("new_cursor"),
                cursor_oldest_date=result["oldest_seen"],
                pages_done=new_pages,
                messages_inserted=new_msg_count,
            )
            # Update local job dict for next iteration
            job["cursor_msg_id"] = result.get("new_cursor")
            job["pages_done"] = new_pages
            job["messages_inserted"] = new_msg_count

            # Light rate-limit — don't hammer Telethon
            await asyncio.sleep(2)

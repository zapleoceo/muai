"""Backfill TG history — пройдись по всем диалогам и подтяни старые сообщения.

Запуск: docker exec vera3-ingestor-telegram python -m ingestor_telegram.backfill
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from telethon import TelegramClient

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow

log = logging.getLogger("tg-backfill")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_DIR = Path(os.environ.get("TELEGRAM_SESSION_DIR", "/sessions"))
SESSION_NAME = "userbot"

# Сколько дней истории качать
DAYS_BACK = int(os.environ.get("BACKFILL_DAYS_BACK", "30"))
# Сколько сообщений на диалог максимум
MAX_PER_DIALOG = int(os.environ.get("BACKFILL_MAX_PER_DIALOG", "500"))


async def backfill_dialog(client: TelegramClient, dialog, cutoff: datetime, me) -> int:
    """Backfill сообщений из одного диалога с момента cutoff."""
    inserted = 0
    try:
        async for msg in client.iter_messages(dialog.id, limit=MAX_PER_DIALOG):
            if not msg.date:
                continue
            if msg.date.replace(tzinfo=None) < cutoff:
                break

            text = (msg.message or "")[:8000]
            if not text:
                continue

            sender = None
            try:
                sender = await msg.get_sender()
            except Exception:
                pass

            sender_name = "(unknown)"
            if sender:
                sender_name = getattr(sender, "first_name", "") or ""
                if getattr(sender, "last_name", None):
                    sender_name += " " + sender.last_name
                username = getattr(sender, "username", None)
                if username:
                    sender_name += f" (@{username})"

            chat_title = getattr(dialog, "title", "(private)") or "(private)"
            direction = "sent" if (sender and sender.id == me.id) else "received"

            content = (
                f"From: {sender_name}\n"
                f"Chat: {chat_title}\n"
                f"Date: {msg.date.isoformat()}\n"
                f"Direction: {direction}\n"
                f"---\n{text}"
            )

            sid = f"tg:{dialog.id}:{msg.id}"

            async with get_session() as s:
                exists = (await s.execute(
                    select(EventRow.id).where(
                        EventRow.source == "telegram",
                        EventRow.source_event_id == sid,
                    )
                )).scalar_one_or_none()
                if exists:
                    continue
                s.add(EventRow(
                    source="telegram",
                    source_event_id=sid,
                    account="userbot",
                    category="message",
                    content_text=content,
                    occurred_at=msg.date.replace(tzinfo=None),
                    metadata_={
                        "chat_id": dialog.id,
                        "chat_title": chat_title,
                        "sender_id": sender.id if sender else None,
                        "sender_username": getattr(sender, "username", None) if sender else None,
                        "direction": direction,
                        "msg_id": msg.id,
                    },
                    triage_status="pending",
                ))
                inserted += 1
    except Exception as e:
        log.warning("Dialog %s failed: %s", dialog.id, e)
    return inserted


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()

    client = TelegramClient(str(SESSION_DIR / SESSION_NAME), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("NOT AUTHORIZED — нужен re-auth с SMS")
        return

    me = await client.get_me()
    cutoff = datetime.utcnow() - timedelta(days=DAYS_BACK)
    log.info("Backfilling TG dialogs since %s for @%s", cutoff.date(), me.username)

    total_inserted = 0
    n_dialogs = 0
    async for dialog in client.iter_dialogs():
        n_dialogs += 1
        title = getattr(dialog, "title", None) or getattr(dialog.entity, "first_name", "?")
        n = await backfill_dialog(client, dialog, cutoff, me)
        total_inserted += n
        log.info("[%s/?] dialog '%s' (id=%s): +%s new (running total: %s)",
                 n_dialogs, title[:30], dialog.id, n, total_inserted)
        # rate-limit friendly
        await asyncio.sleep(1)

    log.info("DONE: %s dialogs, %s new events", n_dialogs, total_inserted)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

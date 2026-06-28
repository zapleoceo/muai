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

            media_kind: str | None = None
            media_meta: dict = {}
            needs_recognition = False
            if getattr(msg, "photo", None):
                media_kind = "photo"; needs_recognition = True
            elif getattr(msg, "voice", None):
                media_kind = "voice"
                media_meta["duration_s"] = getattr(msg.voice, "duration", None)
                needs_recognition = True
            elif getattr(msg, "video_note", None):
                media_kind = "video_note"
                media_meta["duration_s"] = getattr(msg.video_note, "duration", None)
            elif getattr(msg, "video", None):
                media_kind = "video"
                media_meta["duration_s"] = getattr(msg.video, "duration", None)
            elif getattr(msg, "audio", None):
                media_kind = "audio"
                media_meta["duration_s"] = getattr(msg.audio, "duration", None)
                needs_recognition = True
            elif getattr(msg, "sticker", None):
                media_kind = "sticker"
                media_meta["emoji"] = getattr(msg.sticker, "alt", None) or ""
            elif getattr(msg, "document", None):
                media_kind = "document"
                media_meta["mime"] = getattr(msg.document, "mime_type", None)
            elif getattr(msg, "media", None):
                media_kind = type(msg.media).__name__.lower()

            if not text and media_kind:
                text = f"[{media_kind}]"
                if media_meta.get("duration_s"):
                    text = f"[{media_kind}: {media_meta['duration_s']}s]"
                elif media_meta.get("emoji"):
                    text = f"[sticker: {media_meta['emoji']}]"
            elif media_kind:
                text = f"[{media_kind}] {text}"

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
            author_role = "self" if direction == "sent" else "counterparty"
            sender_username = getattr(sender, "username", None) if sender else None
            author_label = (
                "Я"
                if author_role == "self"
                else (f"@{sender_username}" if sender_username else sender_name)
            )

            content = (
                f"Author: {author_label} [{author_role}]\n"
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
                        "sender_username": sender_username,
                        "direction": direction,
                        "author_role": author_role,
                        "author_label": author_label,
                        "msg_id": msg.id,
                        "media_kind": media_kind,
                        "media_meta": media_meta or None,
                        "needs_recognition": needs_recognition,
                    },
                    triage_status="media_pending" if needs_recognition else "pending",
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

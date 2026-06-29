"""Telegram userbot — Telethon с StringSession из БД.

Слушает все incoming/outgoing сообщения из всех диалогов Димы.
Не зависит от файловой системы (нет sqlite3 baga в docker compose).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import TelegramSessionRow
from vera_shared.crypto import decrypt

from ingestor_telegram.entity_sync import sync_message_entities
from ingestor_telegram.tools_http import build_app

log = logging.getLogger("tg")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ["TELEGRAM_PHONE"]


async def load_session_string() -> str:
    async with get_session() as s:
        row = (await s.execute(
            select(TelegramSessionRow)
            .where(TelegramSessionRow.phone == PHONE)
            .where(TelegramSessionRow.is_active.is_(True))
        )).scalar_one_or_none()
    if row is None:
        raise RuntimeError(f"No active session for phone={PHONE}. "
                           "Run scripts/extract_tg_session.py first.")
    return decrypt(row.session_string_enc)


async def save_message(client: TelegramClient, msg) -> None:
    """Сохранить одно сообщение как event source='telegram'."""
    try:
        chat = await msg.get_chat()
        sender = await msg.get_sender()
    except Exception as e:
        log.warning("get_chat/sender failed: %s", e)
        return

    sender_name = "(unknown)"
    sender_username = None
    if sender:
        sender_name = getattr(sender, "first_name", "") or ""
        if getattr(sender, "last_name", None):
            sender_name += " " + sender.last_name
        sender_username = getattr(sender, "username", None)
        if sender_username:
            sender_name += f" (@{sender_username})"

    chat_title = (getattr(chat, "title", None) or
                  getattr(chat, "first_name", None) or "(private)")
    chat_type = type(chat).__name__.lower()
    text = (msg.message or msg.text or "")[:8000]

    # Media — было: пустой text → событие терялось. Стало: всегда плейсхолдер
    # + media_kind в metadata. media_pending → отдельный воркер скачает и
    # распознает (vision/whisper), допишет content_text.
    media_kind: str | None = None
    media_meta: dict[str, Any] = {}
    needs_recognition = False
    if getattr(msg, "photo", None):
        media_kind = "photo"
        needs_recognition = True
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
        needs_recognition = True  # music/podcast also goes through whisper
    elif getattr(msg, "sticker", None):
        media_kind = "sticker"
        media_meta["emoji"] = getattr(msg.sticker, "alt", None) or ""
        _mime = getattr(msg.sticker, "mime_type", "") or ""
        media_meta["mime"] = _mime
        # Static webp stickers are images → vision describes them. Animated
        # (.tgs lottie) and video (.webm) stickers aren't images; the emoji
        # alt-text already in the placeholder is the best signal.
        if _mime == "image/webp":
            needs_recognition = True
    elif getattr(msg, "document", None):
        media_kind = "document"
        media_meta["mime"] = getattr(msg.document, "mime_type", None)
        media_meta["size"] = getattr(msg.document, "size", None)
    elif getattr(msg, "media", None):
        media_kind = type(msg.media).__name__.lower()

    if not text and media_kind:
        text = f"[{media_kind}]"
        if media_meta.get("duration_s"):
            text = f"[{media_kind}: {media_meta['duration_s']}s]"
        elif media_meta.get("emoji"):
            text = f"[sticker: {media_meta['emoji']}]"
    elif media_kind:
        # caption + media: keep both
        text = f"[{media_kind}] {text}"

    if not text:
        # truly empty (no media, no caption) — skip to keep DB clean
        return

    me = await client.get_me()
    direction = "sent" if (sender and sender.id == me.id) else "received"
    author_role = "self" if direction == "sent" else "counterparty"
    author_label = (
        "Я"
        if author_role == "self"
        else (f"@{sender_username}" if sender_username else sender_name)
    )

    # Author: первая строка — однозначный маркер «своё/чужое». chat_title в
    # личке = собеседник; без явного author_role читатели путали его с автором.
    content = (
        f"Author: {author_label} [{author_role}]\n"
        f"From: {sender_name}\n"
        f"Chat: {chat_title} ({chat_type})\n"
        f"Date: {msg.date.isoformat() if msg.date else ''}\n"
        f"Direction: {direction}\n"
        f"---\n{text}"
    )

    source_event_id = f"tg:{chat.id}:{msg.id}"

    async with get_session() as s:
        existing = (await s.execute(
            select(EventRow.id).where(
                EventRow.source == "telegram",
                EventRow.source_event_id == source_event_id,
            )
        )).scalar_one_or_none()
        if existing:
            return
        s.add(EventRow(
            source="telegram",
            source_event_id=source_event_id,
            account="userbot",
            category=chat_type,
            content_text=content,
            occurred_at=msg.date.replace(tzinfo=None) if msg.date else datetime.utcnow(),
            metadata_={
                "chat_id": chat.id,
                "chat_type": chat_type,
                "chat_title": chat_title,
                "sender_id": sender.id if sender else None,
                "sender_username": sender_username,
                "direction": direction,
                "author_role": author_role,
                "author_label": author_label,
                "msg_id": msg.id,
                "is_channel": chat_type in {"channel"},
                "is_group": chat_type in {"chat", "chatfull"},
                "is_supergroup": chat_type == "channel" and getattr(chat, "megagroup", False),
                "media_kind": media_kind,
                "media_meta": media_meta or None,
                "needs_recognition": needs_recognition,
            },
            triage_status="media_pending" if needs_recognition else "pending",
        ))

    # Side-effect: keep entities/memberships in sync with reality.
    try:
        await sync_message_entities(chat, sender)
    except Exception as e:
        log.warning("entity_sync failed: %s", e)


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()

    session_str = await load_session_string()
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("StringSession не авторизован — нужен заново auth с SMS")
        return

    me = await client.get_me()
    log.info("Userbot connected as %s (@%s, id=%s)",
             me.first_name, me.username, me.id)

    @client.on(events.NewMessage(incoming=True, outgoing=True))
    async def on_new(event):
        try:
            await save_message(client, event.message)
        except Exception as e:
            log.warning("Save failed: %s", e)

    log.info("Listening for new messages…")

    # Co-host FastAPI tools server so brain-search can query Telegram live.
    import uvicorn
    app = build_app(client)
    config = uvicorn.Config(app, host="0.0.0.0", port=8000,
                             log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    tools_task = asyncio.create_task(server.serve())
    log.info("Tools HTTP server up on :8000 (/tools/*)")

    # Backfill queue worker — pulls jobs from backfill_jobs table, walks
    # each dialog back to its target_floor_date. Same Telethon client, so
    # no second auth needed. Lives in the same loop — flood-wait pauses
    # don't block live message handling because on_new is event-driven.
    from ingestor_telegram.backfill_worker import backfill_loop
    backfill_task = asyncio.create_task(backfill_loop(client))
    log.info("Backfill queue worker started")

    try:
        await client.run_until_disconnected()
    finally:
        server.should_exit = True
        backfill_task.cancel()
        await tools_task
        with contextlib.suppress(asyncio.CancelledError):
            await backfill_task


if __name__ == "__main__":
    asyncio.run(main())

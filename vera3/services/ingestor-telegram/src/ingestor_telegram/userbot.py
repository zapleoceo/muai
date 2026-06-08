"""Telegram userbot на Telethon — совместимо с сессией Vera 2.0.

Слушает все incoming сообщения из всех диалогов Димы и записывает их
в events table как source=telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from telethon import TelegramClient, events

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow

log = logging.getLogger("tg")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_DIR = Path(os.environ.get("TELEGRAM_SESSION_DIR", "/sessions"))
SESSION_NAME = "userbot"


async def save_message(client: TelegramClient, msg) -> None:
    try:
        chat = await msg.get_chat()
        sender = await msg.get_sender()
    except Exception as e:
        log.warning("get_chat/sender failed: %s", e)
        return

    sender_name = "(unknown)"
    if sender:
        sender_name = getattr(sender, "first_name", "") or ""
        if getattr(sender, "last_name", None):
            sender_name += " " + sender.last_name
        username = getattr(sender, "username", None)
        if username:
            sender_name += f" (@{username})"

    chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or "(private)"
    chat_type = type(chat).__name__.lower()  # user/chat/channel
    text = (msg.message or msg.text or "")[:8000]

    me = await client.get_me()
    direction = "sent" if (sender and sender.id == me.id) else "received"

    content = (
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
            category="message",
            content_text=content,
            occurred_at=msg.date.replace(tzinfo=None) if msg.date else datetime.utcnow(),
            metadata_={
                "chat_id": chat.id,
                "chat_type": chat_type,
                "chat_title": chat_title,
                "sender_id": sender.id if sender else None,
                "sender_username": getattr(sender, "username", None) if sender else None,
                "direction": direction,
                "msg_id": msg.id,
            },
            triage_status="pending",
        ))


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()

    SESSION_DIR.mkdir(exist_ok=True, parents=True)
    session_path = SESSION_DIR / f"{SESSION_NAME}.session"
    log.info("Telethon session path: %s exists=%s", session_path, session_path.exists())

    client = TelegramClient(str(SESSION_DIR / SESSION_NAME), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("NOT AUTHORIZED — session is invalid. Need re-auth with SMS code (manual).")
        return

    me = await client.get_me()
    log.info("Userbot connected as %s (@%s, id=%s)", me.first_name, me.username, me.id)

    @client.on(events.NewMessage(incoming=True, outgoing=True))
    async def on_new(event):
        try:
            await save_message(client, event.message)
        except Exception as e:
            log.warning("Save failed: %s", e)

    log.info("Listening for new messages...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

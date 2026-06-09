"""Telegram userbot — Telethon с StringSession из БД.

Слушает все incoming/outgoing сообщения из всех диалогов Димы.
Не зависит от файловой системы (нет sqlite3 baga в docker compose).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import TelegramSessionRow
from vera_shared.tokens.crypto import decrypt

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
                "msg_id": msg.id,
                "is_channel": chat_type in {"channel"},
                "is_group": chat_type in {"chat", "chatfull"},
                "is_supergroup": chat_type == "channel" and getattr(chat, "megagroup", False),
            },
            triage_status="pending",
        ))


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
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

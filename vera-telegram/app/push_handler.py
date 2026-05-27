"""Real-time push ingestion via Telethon NewMessage events.

Replaces (in steady-state) the 60-second poll loop:
  - latency: 30s avg → <1s
  - covers ALL chats (no _DIALOG_LIMIT cap of 100)
  - covers SENT messages too (outgoing=True)

The periodic poll loop остаётся as recovery — на случай если соединение
обрывалось и события не пришли. Push — основной канал, poll — страховка.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from telethon import events
from telethon.errors import FloodWaitError
from telethon.tl.types import Message

from vera_shared.db.engine import get_session
from vera_shared.db.models import Source
from vera_shared.sources import evaluate
from sqlalchemy import select

from app.poller import _build_payload, _post_event, _refresh_folders
from app.userbot.client import get_client

log = logging.getLogger(__name__)


_registered = False


async def start_push_listener() -> None:
    """Wire NewMessage event handler. Idempotent."""
    global _registered
    if _registered:
        return
    client = get_client()
    if not client.is_connected() or not await client.is_user_authorized():
        log.warning("push_handler: client not ready, skipping registration")
        return
    me = await client.get_me()
    me_id = me.id

    # Find the tg-main source row once at startup — filters change rarely.
    async def _get_source() -> Source | None:
        async with get_session() as s:
            row = (await s.execute(
                select(Source).where(Source.type == "telegram",
                                       Source.enabled == True).limit(1)
            )).scalar_one_or_none()
            return row

    @client.on(events.NewMessage(incoming=True, outgoing=True))
    async def on_new_message(event) -> None:
        try:
            src = await _get_source()
            if src is None:
                return
            msg: Message = event.message
            if not isinstance(msg, Message):
                return
            dialog = await event.get_chat()

            class _DialogShim:
                def __init__(self, entity):
                    self.entity = entity
            dialog_obj = _DialogShim(dialog)

            folder_map = await _refresh_folders()
            is_sent = (getattr(msg, "from_id", None)
                        and getattr(msg.from_id, "user_id", None) == me_id)
            payload = await _build_payload(src.name, dialog_obj, msg, me_id, folder_map)
            payload.setdefault("metadata", {})["direction"] = (
                "sent" if is_sent else "received"
            )
            filter_p = payload.pop("_filter_payload")
            decision = evaluate(src.filters, filter_p)
            if decision == "exclude":
                return
            if decision == "priority":
                payload["category"] = "priority"
            await _post_event(payload)
            log.debug("push: ingested %s msg in chat %s",
                      payload.get("metadata", {}).get("direction"),
                      payload.get("metadata", {}).get("chat_title"))
        except FloodWaitError as exc:
            log.warning("push: flood wait %ds", exc.seconds)
        except Exception as exc:
            log.exception("push: handler error: %s", exc)

    _registered = True
    log.info("push_handler: registered NewMessage listener (in+out)")

"""Telegram poller: scans recent messages across selected dialogs, applies
per-source filters from the `sources` table, POSTs new messages to vera-core
as events."""
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, Message, User

from vera_shared.db.engine import get_session
from vera_shared.db.models import Source
from vera_shared.sources import evaluate

from app.config import get_settings
from app.userbot.client import get_client

log = logging.getLogger(__name__)

_DIALOG_LIMIT = 100
_MSG_LIMIT = 10
_DEFAULT_INTERVAL = 60


def _chat_type(entity) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "private"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    return "unknown"


def _name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    fn = getattr(entity, "first_name", None) or ""
    ln = getattr(entity, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(getattr(entity, "id", "?"))


async def _post_event(payload: dict) -> None:
    cfg = get_settings()
    headers = {"X-Internal-Secret": cfg.internal_secret} if cfg.internal_secret else {}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{cfg.vera_core_url}/event", json=payload, headers=headers)
    if r.status_code != 200:
        log.warning("POST /event failed (%d): %s", r.status_code, r.text[:200])


async def _build_payload(source_name: str, dialog, msg: Message, me_id: int) -> dict:
    entity = dialog.entity
    chat_type = _chat_type(entity)
    chat_id = getattr(entity, "id", None)
    sender = getattr(msg, "sender", None) or entity
    sender_id = getattr(sender, "id", None)
    sender_username = getattr(sender, "username", None)
    sender_name = _name(sender)
    text = (msg.message or "").strip()
    mention_me = False
    if msg.entities:
        for e in msg.entities:
            if hasattr(e, "user_id") and getattr(e, "user_id", None) == me_id:
                mention_me = True
                break
            if hasattr(e, "url"):
                continue
    reply_to_me = False
    if msg.reply_to and msg.reply_to.reply_to_msg_id:
        try:
            replied = await get_client().get_messages(entity, ids=msg.reply_to.reply_to_msg_id)
            if replied and getattr(replied, "from_id", None):
                from_obj = replied.from_id
                if getattr(from_obj, "user_id", None) == me_id:
                    reply_to_me = True
        except Exception:
            pass

    filter_payload = {
        "chat_type": chat_type,
        "chat_id": chat_id,
        "from_user_id": sender_id,
        "from_username": sender_username,
        "from_contact_known": bool(getattr(sender, "contact", False))
                              or bool(getattr(sender, "mutual_contact", False)),
        "mention_me": mention_me,
        "reply_to_me": reply_to_me,
        "text": text,
        "has_attachment": msg.media is not None,
        "now": datetime.utcnow(),
    }

    body = (
        f"From: {sender_name}"
        + (f" (@{sender_username})" if sender_username else "")
        + f"\nChat: {_name(entity)} ({chat_type})"
        + f"\nDate: {msg.date.isoformat() if msg.date else '?'}"
        + f"\n---\n{text or '(пусто)'}"
    )
    entity_hints = [
        {"type": "person", "identifier": sender_username or str(sender_id),
         "name": sender_name, "via": "telegram"},
        {"type": "chat", "identifier": str(chat_id), "name": _name(entity),
         "chat_type": chat_type, "platform": "telegram"},
    ]
    metadata = {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "chat_title": _name(entity),
        "message_id": msg.id,
        "sender_id": sender_id,
        "sender_username": sender_username,
        "mention_me": mention_me,
        "reply_to_me": reply_to_me,
    }
    return {
        "source": "telegram",
        "source_event_id": f"{source_name}:{chat_id}:{msg.id}",
        "account": source_name,
        "category": "communication",
        "content_text": body,
        "entity_hints": entity_hints,
        "metadata": metadata,
        "occurred_at": (msg.date or datetime.now(timezone.utc)).isoformat(),
        "_filter_payload": filter_payload,
    }


async def _process_source(source: Source, me_id: int) -> tuple[int, dict]:
    client = get_client()
    seen_msg = (source.config or {}).get("seen_msg_ids") or {}
    new_seen: dict[str, int] = {str(k): int(v) for k, v in seen_msg.items()}
    ingested = 0
    try:
        async for dialog in client.iter_dialogs(limit=_DIALOG_LIMIT):
            chat_id = str(getattr(dialog.entity, "id", ""))
            if not chat_id:
                continue
            last_seen = int(new_seen.get(chat_id, 0))
            max_id_for_chat = last_seen
            messages = await client.get_messages(dialog.entity, limit=_MSG_LIMIT)
            for msg in messages:
                if not isinstance(msg, Message) or msg.id <= last_seen:
                    continue
                if (getattr(msg, "from_id", None)
                        and getattr(msg.from_id, "user_id", None) == me_id):
                    max_id_for_chat = max(max_id_for_chat, msg.id)
                    continue  # skip own messages
                payload = await _build_payload(source.name, dialog, msg, me_id)
                filter_p = payload.pop("_filter_payload")
                decision = evaluate(source.filters, filter_p)
                if decision == "exclude":
                    max_id_for_chat = max(max_id_for_chat, msg.id)
                    continue
                if decision == "priority":
                    payload["category"] = "priority"
                await _post_event(payload)
                ingested += 1
                max_id_for_chat = max(max_id_for_chat, msg.id)
            if max_id_for_chat > last_seen:
                new_seen[chat_id] = max_id_for_chat
    except FloodWaitError as exc:
        log.warning("Telegram flood wait %ds for source %s", exc.seconds, source.name)
    return ingested, new_seen


async def _persist_source_state(source_id: int, new_seen: dict, ingested: int) -> None:
    async with get_session() as s:
        row = await s.get(Source, source_id)
        if row is None:
            return
        cfg = dict(row.config or {})
        cfg["seen_msg_ids"] = new_seen
        row.config = cfg
        row.last_polled_at = datetime.utcnow()
        row.last_error = None
        row.intake_count = (row.intake_count or 0) + ingested
        await s.commit()


async def poll_loop() -> None:
    log.info("Telegram source poller started")
    while True:
        try:
            client = get_client()
            if not client.is_connected() or not await client.is_user_authorized():
                await asyncio.sleep(10)
                continue
            me = await client.get_me()
            async with get_session() as s:
                result = await s.execute(
                    select(Source).where(Source.type == "telegram", Source.enabled == True)
                )
                sources = result.scalars().all()
            for src in sources:
                try:
                    ingested, new_seen = await _process_source(src, me.id)  # type: ignore[misc]
                    await _persist_source_state(src.id, new_seen, ingested)
                except Exception as exc:
                    log.exception("source %s failed: %s", src.name, exc)
                    async with get_session() as s:
                        row = await s.get(Source, src.id)
                        if row:
                            row.last_error = str(exc)[:500]
                            await s.commit()
        except Exception as exc:
            log.exception("telegram poller iteration crashed: %s", exc)
        await asyncio.sleep(_DEFAULT_INTERVAL)

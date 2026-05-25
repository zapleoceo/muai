"""Telegram poller: scans recent messages across selected dialogs, applies
per-source filters from the `sources` table, POSTs new messages to vera-core
as events."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.functions.users import GetFullUserRequest
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

# Cache folder map (chat_id → folder title) and mutual-chats (user_id →
# [chat titles]) for the whole poller process. Refresh every N minutes.
_FOLDER_CACHE: dict[int, str] = {}
_FOLDER_CACHE_AT: datetime | None = None
_MUTUAL_CACHE: dict[int, list[str]] = {}
_MUTUAL_TTL = timedelta(hours=12)
_MUTUAL_AT: dict[int, datetime] = {}
_FOLDER_TTL = timedelta(minutes=30)


async def _refresh_folders() -> dict[int, str]:
    global _FOLDER_CACHE, _FOLDER_CACHE_AT
    now = datetime.utcnow()
    if _FOLDER_CACHE_AT and (now - _FOLDER_CACHE_AT) < _FOLDER_TTL:
        return _FOLDER_CACHE
    try:
        client = get_client()
        result = await client(GetDialogFiltersRequest())
        mapping: dict[int, str] = {}
        # API result varies between layer versions; try both shapes.
        filters_list = getattr(result, "filters", None) or result
        for f in filters_list:
            title_obj = getattr(f, "title", None)
            if hasattr(title_obj, "text"):
                title = title_obj.text
            else:
                title = title_obj
            if not title:
                continue
            for peer_field in ("include_peers", "pinned_peers"):
                for p in getattr(f, peer_field, None) or []:
                    pid = (getattr(p, "user_id", None)
                           or getattr(p, "chat_id", None)
                           or getattr(p, "channel_id", None))
                    if pid is not None:
                        mapping[int(pid)] = title
        _FOLDER_CACHE = mapping
        _FOLDER_CACHE_AT = now
        log.info("folder cache refreshed: %d entries", len(mapping))
    except Exception as exc:
        log.warning("folder cache refresh failed: %s", exc)
    return _FOLDER_CACHE


async def _mutual_chats_for(user: User) -> list[str]:
    user_id = getattr(user, "id", None)
    if user_id is None:
        return []
    cached_at = _MUTUAL_AT.get(user_id)
    if cached_at and (datetime.utcnow() - cached_at) < _MUTUAL_TTL:
        return _MUTUAL_CACHE.get(user_id, [])
    titles: list[str] = []
    try:
        client = get_client()
        full = await client(GetFullUserRequest(user))
        common_count = getattr(full.full_user, "common_chats_count", 0) or 0
        if common_count > 0:
            from telethon.tl.functions.messages import GetCommonChatsRequest
            res = await client(GetCommonChatsRequest(
                user_id=user, max_id=0, limit=min(common_count, 20),
            ))
            titles = [getattr(c, "title", None) or _name(c)
                      for c in res.chats][:10]
    except Exception as exc:
        log.debug("mutual chats for %s failed: %s", user_id, exc)
    _MUTUAL_CACHE[user_id] = titles
    _MUTUAL_AT[user_id] = datetime.utcnow()
    return titles


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


async def _build_payload(source_name: str, dialog, msg: Message, me_id: int,
                          folder_map: dict[int, str]) -> dict:
    entity = dialog.entity
    chat_type = _chat_type(entity)
    chat_id = getattr(entity, "id", None)
    sender = getattr(msg, "sender", None) or entity
    sender_id = getattr(sender, "id", None)
    sender_username = getattr(sender, "username", None)
    sender_name = _name(sender)
    folder_name = folder_map.get(chat_id) if chat_id is not None else None
    mutual_chats: list[str] = []
    if chat_type == "private" and isinstance(entity, User) and not getattr(entity, "bot", False):
        mutual_chats = await _mutual_chats_for(entity)
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
        "folder": folder_name,
        "mutual_chats": mutual_chats,
    }

    chat_title = _name(entity)
    context_lines = [
        f"From: {sender_name}"
        + (f" (@{sender_username})" if sender_username else ""),
        f"Chat: {chat_title} ({chat_type})"
        + (f", folder «{folder_name}»" if folder_name else ""),
        f"Date: {msg.date.isoformat() if msg.date else '?'}",
    ]
    if mutual_chats:
        context_lines.append(
            "Mutual groups with sender: " + ", ".join(mutual_chats[:8])
        )
    context_lines.append("---")
    context_lines.append(text or "(пусто)")
    body = "\n".join(context_lines)

    entity_hints = [
        {"type": "person", "identifier": sender_username or str(sender_id),
         "name": sender_name, "via": "telegram"},
        {"type": "chat", "identifier": str(chat_id), "name": chat_title,
         "chat_type": chat_type, "platform": "telegram"},
    ]
    if folder_name:
        entity_hints.append(
            {"type": "folder", "identifier": folder_name, "platform": "telegram"}
        )
    for g in mutual_chats[:5]:
        entity_hints.append(
            {"type": "chat", "identifier": g, "name": g,
             "chat_type": "shared_with_sender", "platform": "telegram"}
        )

    metadata = {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "chat_title": chat_title,
        "folder": folder_name,
        "message_id": msg.id,
        "sender_id": sender_id,
        "sender_username": sender_username,
        "mention_me": mention_me,
        "reply_to_me": reply_to_me,
        "mutual_chats": mutual_chats,
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


async def _process_source(source: Source, me_id: int,
                           folder_map: dict[int, str]) -> tuple[int, dict]:
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
                is_sent = (getattr(msg, "from_id", None)
                            and getattr(msg.from_id, "user_id", None) == me_id)
                # Keep sent messages too — sent IS the strongest learning
                # signal: that's what Дима actually wrote. Tag direction.
                payload = await _build_payload(source.name, dialog, msg, me_id, folder_map)
                payload.setdefault("metadata", {})["direction"] = (
                    "sent" if is_sent else "received"
                )
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
                    folder_map = await _refresh_folders()
                    ingested, new_seen = await _process_source(src, me.id, folder_map)  # type: ignore[misc]
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

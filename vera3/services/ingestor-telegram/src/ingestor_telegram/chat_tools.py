"""Инструменты агента поверх живого Telethon-клиента: диалоги, участники,
история, поиск пользователя. Без HTTP — маршруты в tools_http.py."""
from __future__ import annotations

from typing import Any

from telethon import TelegramClient
from telethon.errors import ChatAdminRequiredError
from telethon.tl.types import Channel, Chat, User


async def resolve_chat(client: TelegramClient, query: str) -> Any | None:
    """Resolve chat by username/id/title substring. Returns Entity or None."""
    if query.startswith("@"):
        return await client.get_entity(query)
    if query.lstrip("-").isdigit():
        return await client.get_entity(int(query))
    q = query.lower()
    async for d in client.iter_dialogs():
        title = (getattr(d.entity, "title", None)
                 or getattr(d.entity, "first_name", None) or "")
        if q in title.lower():
            return d.entity
    return None


def entity_summary(e: Any) -> dict[str, Any]:
    if isinstance(e, User):
        return {
            "type": "user", "id": e.id,
            "username": e.username,
            "first_name": e.first_name, "last_name": e.last_name,
            "is_bot": e.bot, "is_self": e.is_self,
        }
    if isinstance(e, Channel):
        return {
            "type": "supergroup" if e.megagroup else "channel",
            "id": e.id, "username": e.username,
            "title": e.title,
            "participants_count": getattr(e, "participants_count", None),
        }
    if isinstance(e, Chat):
        return {"type": "group", "id": e.id, "title": e.title,
                "participants_count": getattr(e, "participants_count", None)}
    return {"type": type(e).__name__.lower(), "id": getattr(e, "id", None)}


async def list_dialogs(client: TelegramClient, q: str, limit: int) -> dict[str, Any]:
    out = []
    async for d in client.iter_dialogs(limit=200 if q else limit):
        title = (getattr(d.entity, "title", None)
                 or getattr(d.entity, "first_name", None) or "")
        if q and q not in title.lower():
            continue
        out.append({
            **entity_summary(d.entity),
            "title": title,
            "unread_count": d.unread_count,
            "last_message_date": d.date.isoformat() if d.date else None,
        })
        if len(out) >= limit:
            break
    return {"dialogs": out, "count": len(out)}


async def get_chat_info(client: TelegramClient, chat_query: str) -> dict[str, Any]:
    chat = await resolve_chat(client, chat_query)
    if chat is None:
        return {"error": "chat not found", "query": chat_query}
    return entity_summary(chat)


async def get_participants(client: TelegramClient, chat_query: str,
                           limit: int) -> dict[str, Any]:
    chat = await resolve_chat(client, chat_query)
    if chat is None:
        return {"error": "chat not found", "query": chat_query}
    try:
        participants = await client.get_participants(chat, limit=limit)
    except ChatAdminRequiredError:
        return {"error": "admin rights required to list participants",
                "chat": entity_summary(chat)}
    except Exception as e:
        # Ответ агенту, а не 500: он должен увидеть причину и выбрать другой чат.
        return {"error": f"{type(e).__name__}: {e}", "chat": entity_summary(chat)}
    members = [
        {"id": p.id, "username": getattr(p, "username", None),
         "first_name": getattr(p, "first_name", None),
         "last_name": getattr(p, "last_name", None),
         "is_bot": getattr(p, "bot", False),
         "is_self": getattr(p, "is_self", False)}
        for p in participants
    ]
    return {"chat": entity_summary(chat), "members": members, "count": len(members)}


async def get_dialog_history(client: TelegramClient, chat_query: str,
                             limit: int) -> dict[str, Any]:
    chat = await resolve_chat(client, chat_query)
    if chat is None:
        return {"error": "chat not found"}
    me = await client.get_me()
    msgs = []
    async for m in client.iter_messages(chat, limit=limit):
        if not (m.message or m.text):
            continue
        sender_id = getattr(m, "sender_id", None) or 0
        msgs.append({
            "id": m.id,
            "from_self": sender_id == me.id,
            "text": (m.message or m.text or "")[:2000],
            "date": m.date.isoformat() if m.date else None,
        })
    return {"chat": entity_summary(chat), "messages": msgs}


async def find_user(client: TelegramClient, q: str) -> dict[str, Any]:
    try:
        ent = await client.get_entity(q if q.startswith("@") else f"@{q.lstrip('@')}")
        return entity_summary(ent)
    except Exception as e:
        # Ответ агенту, а не 500: «нет такого username» — нормальный исход.
        return {"error": f"{type(e).__name__}: {e}"}

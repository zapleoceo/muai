"""FastAPI tools-server inside the userbot container.

Shares the same Telethon client with the live userbot loop, so the agent
can look up dialogs/participants/history in real time without spinning up
a second auth.

Exposed (all POST, JSON body, X-Internal-Secret required):
  /tools/list_dialogs        {q?: str, limit?: int}
  /tools/get_chat_info       {chat_query: str}
  /tools/get_participants    {chat_query: str, limit?: int}
  /tools/get_dialog_history  {chat_query: str, limit?: int}
  /tools/find_user           {q: str}
  /tools/spec                — returns JSON-Schema list of all tools
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from telethon import TelegramClient
from telethon.errors import ChatAdminRequiredError
from telethon.tl.types import Channel, Chat, User

log = logging.getLogger("tg.tools")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


def _check_secret(x_internal_secret: str | None) -> None:
    if not INTERNAL_SECRET or x_internal_secret != INTERNAL_SECRET:
        raise HTTPException(status_code=401, detail="X-Internal-Secret required")


async def _resolve_chat(client: TelegramClient, query: str):
    """Resolve chat by username/id/title substring. Returns Entity or None."""
    if query.startswith("@"):
        return await client.get_entity(query)
    if query.lstrip("-").isdigit():
        return await client.get_entity(int(query))
    # title substring — search through dialogs
    q = query.lower()
    async for d in client.iter_dialogs():
        title = (getattr(d.entity, "title", None)
                 or getattr(d.entity, "first_name", None) or "")
        if q in title.lower():
            return d.entity
    return None


def _entity_summary(e: Any) -> dict[str, Any]:
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


def build_app(client: TelegramClient) -> FastAPI:
    app = FastAPI(title="ingestor-telegram tools")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "service": "ingestor-telegram-tools"}

    @app.get("/tools/spec")
    async def spec() -> list[dict[str, Any]]:
        return TOOL_SPECS

    @app.post("/tools/list_dialogs")
    async def list_dialogs(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        q = (body.get("q") or "").lower()
        limit = int(body.get("limit") or 50)
        out = []
        async for d in client.iter_dialogs(limit=200 if q else limit):
            title = (getattr(d.entity, "title", None)
                     or getattr(d.entity, "first_name", None) or "")
            if q and q not in title.lower():
                continue
            out.append({
                **_entity_summary(d.entity),
                "title": title,
                "unread_count": d.unread_count,
                "last_message_date": d.date.isoformat() if d.date else None,
            })
            if len(out) >= limit:
                break
        return {"dialogs": out, "count": len(out)}

    @app.post("/tools/get_chat_info")
    async def get_chat_info(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        chat = await _resolve_chat(client, body["chat_query"])
        if chat is None:
            return {"error": "chat not found", "query": body["chat_query"]}
        return _entity_summary(chat)

    @app.post("/tools/get_participants")
    async def get_participants(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        chat = await _resolve_chat(client, body["chat_query"])
        if chat is None:
            return {"error": "chat not found", "query": body["chat_query"]}
        limit = int(body.get("limit") or 200)
        try:
            participants = await client.get_participants(chat, limit=limit)
        except ChatAdminRequiredError:
            return {"error": "admin rights required to list participants",
                    "chat": _entity_summary(chat)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}",
                    "chat": _entity_summary(chat)}
        members = [
            {"id": p.id, "username": getattr(p, "username", None),
             "first_name": getattr(p, "first_name", None),
             "last_name": getattr(p, "last_name", None),
             "is_bot": getattr(p, "bot", False),
             "is_self": getattr(p, "is_self", False)}
            for p in participants
        ]
        return {
            "chat": _entity_summary(chat),
            "members": members,
            "count": len(members),
        }

    @app.post("/tools/get_dialog_history")
    async def get_dialog_history(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        chat = await _resolve_chat(client, body["chat_query"])
        if chat is None:
            return {"error": "chat not found"}
        limit = int(body.get("limit") or 30)
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
        return {"chat": _entity_summary(chat), "messages": msgs}

    @app.post("/tools/find_user")
    async def find_user(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        q = body["q"]
        try:
            ent = await client.get_entity(q if q.startswith("@") else f"@{q.lstrip('@')}")
            return _entity_summary(ent)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    @app.post("/media/download")
    async def media_download(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        """Download media bytes for (chat_id, msg_id). Returns base64+mime.

        Used by media-worker to grab photo/voice/audio for vision/whisper.
        Returns up to 25 MB; larger files refused (Whisper hard limit).
        """
        from base64 import b64encode

        _check_secret(x_internal_secret)
        chat_id = int(body["chat_id"])
        msg_id = int(body["msg_id"])
        try:
            msg = await client.get_messages(chat_id, ids=msg_id)
            if msg is None:
                return {"error": "message not found"}
            if not getattr(msg, "media", None):
                return {"error": "no media on this message"}
            data = await msg.download_media(file=bytes)
            if data is None:
                return {"error": "download returned None (deleted?)"}
            size = len(data)
            if size > 25 * 1024 * 1024:
                return {"error": f"too large: {size} bytes (>25MB)"}
            mime = None
            if getattr(msg, "voice", None):
                mime = "audio/ogg"
            elif getattr(msg, "audio", None):
                mime = getattr(msg.audio, "mime_type", "audio/mpeg")
            elif getattr(msg, "photo", None):
                mime = "image/jpeg"
            elif getattr(msg, "sticker", None):
                mime = getattr(msg.sticker, "mime_type", "image/webp")
            elif getattr(msg, "document", None):
                mime = getattr(msg.document, "mime_type", "application/octet-stream")
            return {"b64": b64encode(data).decode("ascii"), "mime": mime, "size": size}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    return app


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "telegram.list_dialogs",
        "description": "List the user's Telegram dialogs (DM, groups, channels). Optional fuzzy query filters by title substring (case-insensitive). Use this to find a chat by name before calling get_participants or get_dialog_history.",
        "params_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Optional title substring"},
                "limit": {"type": "integer", "default": 50, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "telegram.get_chat_info",
        "description": "Get info about a Telegram chat (type, id, title, participants_count). Accepts @username, numeric id, or title substring.",
        "params_schema": {
            "type": "object",
            "properties": {"chat_query": {"type": "string"}},
            "required": ["chat_query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "telegram.get_participants",
        "description": "List members of a Telegram group / supergroup / channel. Requires the user to be a member; admin role required for some private channels.",
        "params_schema": {
            "type": "object",
            "properties": {
                "chat_query": {"type": "string", "description": "@username, numeric id, or title substring"},
                "limit": {"type": "integer", "default": 200, "maximum": 1000},
            },
            "required": ["chat_query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "telegram.get_dialog_history",
        "description": "Fetch recent messages from a specific Telegram dialog. Returns last N messages with text and sender direction.",
        "params_schema": {
            "type": "object",
            "properties": {
                "chat_query": {"type": "string"},
                "limit": {"type": "integer", "default": 30, "maximum": 200},
            },
            "required": ["chat_query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "telegram.find_user",
        "description": "Resolve a Telegram username to a user profile.",
        "params_schema": {
            "type": "object",
            "properties": {"q": {"type": "string", "description": "Username with or without leading @"}},
            "required": ["q"],
            "additionalProperties": False,
        },
    },
]

"""Cache Telegram dialogs in SQLite so search is instant.

Без кеша каждый search_dialogs / resolve_peer делал iter_dialogs (тяжёлый
запрос к TG, до 60s, плюс FloodWait). С кешем — обычный SQL LIKE по
локальной таблице.

Refresh:
  - При старте сервиса (полный обход dialogs)
  - Каждые 30 минут (фоновая задача)
  - При получении NewMessage (lazy update этого peer'а)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select, update
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User

from vera_shared.db.engine import get_session
from vera_shared.db.models import TgDialog

from app.userbot.client import get_client

log = logging.getLogger(__name__)

_REFRESH_INTERVAL = 30 * 60  # 30 минут


def _type(entity) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
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
    return " ".join(filter(None, [fn, ln])) or str(getattr(entity, "id", ""))


async def _folder_map() -> dict[int, list[str]]:
    """peer_id → list of folder titles."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    out: dict[int, list[str]] = {}
    try:
        client = get_client()
        result = await client(GetDialogFiltersRequest())
        filters_list = getattr(result, "filters", None) or result
        for f in filters_list:
            t = getattr(f, "title", None)
            title = getattr(t, "text", t) if t else None
            if not title:
                continue
            for field in ("include_peers", "pinned_peers"):
                for p in getattr(f, field, None) or []:
                    pid = (getattr(p, "user_id", None)
                           or getattr(p, "chat_id", None)
                           or getattr(p, "channel_id", None))
                    if pid is not None:
                        out.setdefault(int(pid), []).append(title)
    except Exception as exc:
        log.warning("folder map failed: %s", exc)
    return out


async def refresh_all() -> int:
    """Full scan: walk all dialogs, upsert into tg_dialogs. Returns count."""
    client = get_client()
    if not client.is_connected() or not await client.is_user_authorized():
        log.warning("dialog_cache: client not ready")
        return 0
    folders = await _folder_map()
    n = 0
    try:
        async with get_session() as s:
            async for d in client.iter_dialogs():
                pid = int(d.entity.id)
                row = await s.get(TgDialog, pid)
                if row is None:
                    row = TgDialog(id=pid)
                    s.add(row)
                row.name = _name(d.entity)
                row.type = _type(d.entity)
                row.username = getattr(d.entity, "username", None)
                row.folders = folders.get(pid) or []
                row.unread_count = d.unread_count or 0
                row.last_message_date = d.date.replace(tzinfo=None) if d.date else None
                n += 1
                if n % 100 == 0:
                    await s.commit()
            await s.commit()
    except FloodWaitError as exc:
        log.warning("dialog_cache: flood wait %ds during refresh", exc.seconds)
    log.info("dialog_cache: refreshed %d dialogs", n)
    return n


async def upsert_one(entity, folders_for_id: list[str] | None = None,
                      unread: int = 0, last_date: datetime | None = None) -> None:
    """Lazy update for a single peer (called on NewMessage)."""
    if entity is None or not hasattr(entity, "id"):
        return
    pid = int(entity.id)
    async with get_session() as s:
        row = await s.get(TgDialog, pid)
        if row is None:
            row = TgDialog(id=pid)
            s.add(row)
        row.name = _name(entity)
        row.type = _type(entity)
        row.username = getattr(entity, "username", None)
        if folders_for_id is not None:
            row.folders = folders_for_id
        row.unread_count = unread
        if last_date:
            row.last_message_date = last_date.replace(tzinfo=None) if last_date.tzinfo else last_date
        await s.commit()


async def refresh_loop() -> None:
    """Background: full refresh every _REFRESH_INTERVAL."""
    while True:
        try:
            await refresh_all()
        except Exception as exc:
            log.exception("dialog_cache refresh: %s", exc)
        await asyncio.sleep(_REFRESH_INTERVAL)


async def search_cached(query: str, limit: int = 15) -> list[dict]:
    """Substring + folder match against cache. Mirrors original
    search_dialogs response shape so callers don't need changes."""
    q = "".join(c for c in query.lower() if c.isalnum())
    if not q:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            select(TgDialog).order_by(TgDialog.last_message_date.desc())
        )).scalars().all()
    out: list[dict] = []
    for r in rows:
        name_n = "".join(c for c in (r.name or "").lower() if c.isalnum())
        folder_match = any(q in "".join(c for c in (f or "").lower() if c.isalnum())
                            for f in (r.folders or []))
        if q in name_n or folder_match:
            out.append({
                "id": r.id,
                "name": r.name,
                "type": r.type,
                "username": r.username,
                "folders": r.folders or [],
                "match": "folder" if folder_match and q not in name_n else "title",
                "unread_count": r.unread_count,
                "last_message_date": r.last_message_date.isoformat() if r.last_message_date else None,
            })
            if len(out) >= limit:
                break
    return out

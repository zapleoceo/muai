"""
Chat sync configuration service.

Global settings (type filters + blacklist) are stored in the `settings` table as JSON.
Per-chat config lives in `chat_sync_config`.
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, ChatSyncConfig, ChatTopic, Message, Setting

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "sync_settings"
_DEFAULT_SETTINGS: dict = {
    "allowed_types": ["private", "group", "supergroup", "channel"],
    "blacklist": [],          # list of chat_ids (int) or @usernames (str)
    "default_depth_days": 7,
}


# ── Global sync settings ──────────────────────────────────────────────────────

async def get_global_settings() -> dict:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(Setting).where(Setting.key == _SETTINGS_KEY)
        )).scalar_one_or_none()
    if row:
        try:
            return {**_DEFAULT_SETTINGS, **json.loads(row.value)}
        except Exception:
            pass
    return dict(_DEFAULT_SETTINGS)


async def update_global_settings(patch: dict) -> dict:
    current = await get_global_settings()
    current.update(patch)
    async with AsyncSessionLocal() as session:
        stmt = (
            insert(Setting)
            .values(key=_SETTINGS_KEY, value=json.dumps(current))
            .on_conflict_do_update(index_elements=["key"], set_={"value": json.dumps(current)})
        )
        await session.execute(stmt)
        await session.commit()
    return current


# ── Per-chat config ───────────────────────────────────────────────────────────

async def get_chat_config(chat_id: int) -> ChatSyncConfig | None:
    async with AsyncSessionLocal() as session:
        return (await session.execute(
            select(ChatSyncConfig).where(ChatSyncConfig.chat_id == chat_id)
        )).scalar_one_or_none()


async def create_pending(chat_id: int) -> None:
    """Register a new chat as pending (enabled=False, no approved_at)."""
    async with AsyncSessionLocal() as session:
        stmt = (
            insert(ChatSyncConfig)
            .values(chat_id=chat_id, enabled=False, approved_at=None)
            .on_conflict_do_nothing(index_elements=["chat_id"])
        )
        await session.execute(stmt)
        await session.commit()


async def approve_chat(chat_id: int, depth_days: int | None = None) -> None:
    async with AsyncSessionLocal() as session:
        stmt = (
            insert(ChatSyncConfig)
            .values(
                chat_id=chat_id,
                enabled=True,
                approved_at=datetime.now(tz=timezone.utc),
                depth_days=depth_days,
                skip_reason=None,
            )
            .on_conflict_do_update(
                index_elements=["chat_id"],
                set_={
                    "enabled": True,
                    "approved_at": datetime.now(tz=timezone.utc),
                    "depth_days": depth_days,
                    "skip_reason": None,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()


async def disable_chat(chat_id: int, reason: str = "") -> None:
    async with AsyncSessionLocal() as session:
        stmt = (
            insert(ChatSyncConfig)
            .values(chat_id=chat_id, enabled=False, skip_reason=reason)
            .on_conflict_do_update(
                index_elements=["chat_id"],
                set_={"enabled": False, "skip_reason": reason},
            )
        )
        await session.execute(stmt)
        await session.commit()


async def is_blacklisted(chat_id: int, username: str | None, settings: dict) -> bool:
    bl = settings.get("blacklist", [])
    if chat_id in bl:
        return True
    if username and (username in bl or f"@{username}" in bl):
        return True
    return False


async def type_allowed(chat_type: str, settings: dict) -> bool:
    return chat_type in settings.get("allowed_types", _DEFAULT_SETTINGS["allowed_types"])


async def list_chats_with_config() -> list[dict]:
    """Return all chats joined with their sync config, message count, and topics."""
    async with AsyncSessionLocal() as session:
        msg_count_sub = (
            select(Message.chat_id, func.count(Message.id).label("cnt"))
            .group_by(Message.chat_id)
            .subquery()
        )
        rows = (await session.execute(
            select(Chat, ChatSyncConfig, msg_count_sub.c.cnt)
            .outerjoin(ChatSyncConfig, Chat.id == ChatSyncConfig.chat_id)
            .outerjoin(msg_count_sub, Chat.id == msg_count_sub.c.chat_id)
            .order_by(Chat.title)
        )).all()

        topic_rows = (await session.execute(
            select(ChatTopic).order_by(ChatTopic.chat_id, ChatTopic.topic_id)
        )).scalars().all()

    topics_by_chat: dict[int, list[dict]] = {}
    for t in topic_rows:
        topics_by_chat.setdefault(t.chat_id, []).append({
            "id": t.topic_id,
            "title": t.title,
            "is_closed": t.is_closed,
        })

    result = []
    for chat, cfg, msg_cnt in rows:
        if cfg is None:
            status = "unknown"
        elif cfg.enabled:
            status = "active"
        elif cfg.approved_at is None:
            status = "pending"
        else:
            status = "disabled"

        result.append({
            "id": chat.id,
            "type": chat.type,
            "title": chat.title or str(chat.id),
            "username": chat.username,
            "folder": chat.folder,
            "status": status,
            "enabled": cfg.enabled if cfg else False,
            "depth_days": cfg.depth_days if cfg else None,
            "skip_reason": cfg.skip_reason if cfg else None,
            "approved_at": cfg.approved_at.isoformat() if cfg and cfg.approved_at else None,
            "last_synced_at": cfg.last_synced_at.isoformat() if cfg and cfg.last_synced_at else None,
            "message_count": msg_cnt or 0,
            "topics": topics_by_chat.get(chat.id, []),
        })
    return result


async def update_chat_depth(chat_id: int, depth_days: int | None) -> None:
    async with AsyncSessionLocal() as session:
        stmt = (
            insert(ChatSyncConfig)
            .values(chat_id=chat_id, depth_days=depth_days)
            .on_conflict_do_update(index_elements=["chat_id"], set_={"depth_days": depth_days})
        )
        await session.execute(stmt)
        await session.commit()


async def delete_chat_messages(chat_id: int) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(Message).where(Message.chat_id == chat_id)
        )
        await session.commit()
        return result.rowcount


async def auto_approve_existing_chats() -> int:
    """On first deploy, approve all chats already in DB that have no config entry."""
    async with AsyncSessionLocal() as session:
        existing_ids = (await session.execute(
            select(Chat.id).where(
                ~Chat.id.in_(select(ChatSyncConfig.chat_id))
            )
        )).scalars().all()

        if not existing_ids:
            return 0

        now = datetime.now(tz=timezone.utc)
        await session.execute(
            insert(ChatSyncConfig).values([
                {"chat_id": cid, "enabled": True, "approved_at": now}
                for cid in existing_ids
            ]).on_conflict_do_nothing()
        )
        await session.commit()
        logger.info("chat_settings: auto-approved %d existing chats", len(existing_ids))
        return len(existing_ids)

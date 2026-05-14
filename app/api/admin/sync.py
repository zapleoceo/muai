from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_owner
from app.services.sync_manager import get_sync_manager

router = APIRouter()


@router.post("/admin/sync/stop")
async def stop_sync(_uid: int = Depends(require_owner)) -> dict:
    get_sync_manager().stop_all()
    return {"ok": True, "action": "sync-stopped"}


@router.get("/admin/sync/queue")
async def sync_queue(_uid: int = Depends(require_owner)) -> dict:
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.db.database import AsyncSessionLocal
    from app.db.models import Chat, ChatSyncConfig
    from app.services.chat_settings import get_global_settings, is_blacklisted, type_allowed
    mgr = get_sync_manager()
    settings = await get_global_settings()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Chat.id, Chat.title, Chat.username, Chat.type, ChatSyncConfig.depth_days, ChatSyncConfig.last_synced_at)
            .join(ChatSyncConfig, Chat.id == ChatSyncConfig.chat_id)
            .where(ChatSyncConfig.enabled.is_(True))
            .order_by(ChatSyncConfig.last_synced_at.asc().nullsfirst(), Chat.title.asc().nulls_last(), Chat.id.asc())
        )).all()
    current = mgr.status.current_chat
    now = datetime.now(tz=timezone.utc)
    queue = []
    excluded = 0
    for r in rows:
        if not await type_allowed(r.type, settings):
            excluded += 1
            continue
        if await is_blacklisted(int(r.id), r.username, settings):
            excluded += 1
            continue
        last = r.last_synced_at
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        queue.append(
            {
                "id": int(r.id),
                "title": r.title or "—",
                "username": r.username,
                "type": r.type,
                "depth_days": r.depth_days,
                "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
                "active": (r.title == current),
                "stale": True if (last is None) else ((now - last) > timedelta(minutes=10)),
            }
        )
    return {
        "running": mgr.status.running,
        "current_chat": current,
        "chats_done": mgr.status.chats_done,
        "excluded": excluded,
        "queue": queue,
    }


@router.post("/admin/sync/start")
async def start_sync(_uid: int = Depends(require_owner)) -> dict:
    import asyncio
    from app.config import get_settings
    from app.userbot.client import get_client
    from app.userbot.sync import sync_history
    mgr = get_sync_manager()
    if mgr.status.running:
        return {"ok": False, "detail": "already running"}
    client = get_client()
    task = asyncio.create_task(sync_history(client, days=get_settings().sync_history_days))
    mgr.set_task(task)
    return {"ok": True, "action": "sync-started"}


@router.get("/admin/sync/status")
async def sync_status(_uid: int = Depends(require_owner)) -> dict:
    mgr = get_sync_manager()
    s = mgr.status
    return {
        "running": s.running,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "current_chat": s.current_chat,
        "chats_done": s.chats_done,
        "messages_saved": s.messages_saved,
        "syncing_chat_ids": list(mgr.get_syncing_chat_ids()),
    }

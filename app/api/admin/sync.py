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
    from sqlalchemy import select
    from app.db.database import AsyncSessionLocal
    from app.db.models import Chat, ChatSyncConfig
    mgr = get_sync_manager()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Chat.title, Chat.type, ChatSyncConfig.depth_days)
            .join(ChatSyncConfig, Chat.id == ChatSyncConfig.chat_id)
            .where(ChatSyncConfig.enabled.is_(True))
            .order_by(Chat.title)
        )).all()
    current = mgr.status.current_chat
    return {
        "running": mgr.status.running,
        "current_chat": current,
        "chats_done": mgr.status.chats_done,
        "queue": [
            {"title": r.title or "—", "type": r.type, "depth_days": r.depth_days, "active": (r.title == current)}
            for r in rows
        ],
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
    s = get_sync_manager().status
    return {
        "running": s.running,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "current_chat": s.current_chat,
        "chats_done": s.chats_done,
        "messages_saved": s.messages_saved,
    }

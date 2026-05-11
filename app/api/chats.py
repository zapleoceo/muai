import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import require_owner
from app.services import chat_settings as cs
from app.services.sync_manager import get_sync_manager

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Chat list ─────────────────────────────────────────────────────────────────

@router.get("/admin/chats")
async def list_chats(_uid: int = Depends(require_owner)) -> list[dict]:
    return await cs.list_chats_with_config()


# ── Per-chat actions ──────────────────────────────────────────────────────────

class ApproveBody(BaseModel):
    depth_days: int | None = None


@router.post("/admin/chats/{chat_id}/approve")
async def approve_chat(
    chat_id: int,
    body: ApproveBody = ApproveBody(),
    _uid: int = Depends(require_owner),
) -> dict:
    await cs.approve_chat(chat_id, body.depth_days)
    return {"ok": True, "chat_id": chat_id, "action": "approved"}


class SkipBody(BaseModel):
    reason: str = ""


@router.post("/admin/chats/{chat_id}/skip")
async def skip_chat(
    chat_id: int,
    body: SkipBody = SkipBody(),
    _uid: int = Depends(require_owner),
) -> dict:
    await cs.disable_chat(chat_id, body.reason)
    return {"ok": True, "chat_id": chat_id, "action": "skipped"}


@router.post("/admin/chats/{chat_id}/cancel-sync")
async def cancel_sync(chat_id: int, _uid: int = Depends(require_owner)) -> dict:
    get_sync_manager().cancel_chat(chat_id)
    return {"ok": True, "chat_id": chat_id, "action": "cancel-requested"}


class ChatPatch(BaseModel):
    depth_days: int | None = None


@router.patch("/admin/chats/{chat_id}")
async def patch_chat(
    chat_id: int,
    body: ChatPatch,
    _uid: int = Depends(require_owner),
) -> dict:
    await cs.update_chat_depth(chat_id, body.depth_days)
    return {"ok": True, "chat_id": chat_id, "depth_days": body.depth_days}


@router.delete("/admin/chats/{chat_id}/messages")
async def delete_messages(chat_id: int, _uid: int = Depends(require_owner)) -> dict:
    deleted = await cs.delete_chat_messages(chat_id)
    return {"deleted": deleted, "chat_id": chat_id}


# ── Global sync control ───────────────────────────────────────────────────────

@router.post("/admin/sync/stop")
async def stop_sync(_uid: int = Depends(require_owner)) -> dict:
    get_sync_manager().stop_all()
    return {"ok": True, "action": "sync-stopped"}


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


# ── Global sync settings ──────────────────────────────────────────────────────

@router.get("/admin/settings/sync")
async def get_sync_settings(_uid: int = Depends(require_owner)) -> dict:
    return await cs.get_global_settings()


class SyncSettingsPatch(BaseModel):
    allowed_types: list[str] | None = None
    blacklist: list[str | int] | None = None
    default_depth_days: int | None = None


@router.patch("/admin/settings/sync")
async def update_sync_settings(
    body: SyncSettingsPatch,
    _uid: int = Depends(require_owner),
) -> dict:
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")
    return await cs.update_global_settings(patch)

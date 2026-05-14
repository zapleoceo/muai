import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.auth import require_owner
from app.services import chat_settings as cs
from app.services.sync_manager import get_sync_manager
from app.userbot import folders as folders_svc

router = APIRouter()

_MAX_AVATAR_CACHE = 500
_avatar_cache: dict[int, bytes | None] = {}  # evict oldest when over limit


def _cache_avatar(chat_id: int, data: bytes | None) -> None:
    if len(_avatar_cache) >= _MAX_AVATAR_CACHE:
        # evict oldest entry
        _avatar_cache.pop(next(iter(_avatar_cache)))
    _avatar_cache[chat_id] = data


@router.get("/admin/chats")
async def list_chats(_uid: int = Depends(require_owner)) -> list[dict]:
    return await cs.list_chats_with_config()


@router.get("/admin/chats/{chat_id}/avatar")
async def chat_avatar(chat_id: int, _uid: int = Depends(require_owner)) -> Response:
    if chat_id not in _avatar_cache:
        from app.userbot.client import get_client
        client = get_client()
        try:
            buf = io.BytesIO()
            await client.download_profile_photo(chat_id, file=buf)
            data = buf.getvalue()
            _cache_avatar(chat_id, data if data else None)
        except Exception:
            _cache_avatar(chat_id, None)
    data = _avatar_cache.get(chat_id)
    if not data:
        raise HTTPException(status_code=404)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


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


@router.post("/admin/chats/{chat_id}/sync-now")
async def sync_chat_now(chat_id: int, _uid: int = Depends(require_owner)) -> dict:
    import asyncio
    from app.userbot.sync import sync_single_chat
    asyncio.create_task(sync_single_chat(chat_id))
    return {"ok": True, "chat_id": chat_id}


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


@router.post("/admin/chats/sync-folders")
async def sync_folders(_uid: int = Depends(require_owner)) -> dict:
    try:
        updated = await folders_svc.sync_folders()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"updated": updated}


@router.post("/admin/chats/sync-topics")
async def sync_topics(_uid: int = Depends(require_owner)) -> dict:
    from app.userbot import topics as topics_svc
    try:
        updated = await topics_svc.sync_topics()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"updated": updated}

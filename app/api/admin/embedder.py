from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from app.api.auth import require_owner
from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.services.embedder import get_embedder_status, start_embedder, stop_embedder
from app.services.media_embedder import get_media_embedder_manager

router = APIRouter()


@router.get("/admin/embedder/status")
async def embedder_status(_uid: int = Depends(require_owner)) -> dict:
    async with AsyncSessionLocal() as session:
        stats = await MessageRepo(session).chunk_stats()
    return {**get_embedder_status(), **stats}


@router.post("/admin/embedder/restart")
async def embedder_restart(_uid: int = Depends(require_owner)) -> dict:
    start_embedder()
    return {"status": "started"}


@router.post("/admin/embedder/stop")
async def embedder_stop(_uid: int = Depends(require_owner)) -> dict:
    stop_embedder()
    return {"status": "stopped"}


@router.delete("/admin/embedder/chunks")
async def clear_chunks(_uid: int = Depends(require_owner)) -> dict:
    stop_embedder()
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("DELETE FROM message_chunks"))
        await session.commit()
    return {"deleted": result.rowcount}


class MediaStartBody(BaseModel):
    types: list[str] = []


@router.get("/admin/media-embedder/status")
async def media_embedder_status(_uid: int = Depends(require_owner)) -> dict:
    mgr = get_media_embedder_manager()
    stats = await mgr.get_stats()
    s = mgr.status
    return {
        "running": s.running,
        "enabled": s.enabled,
        "current_item": s.current_item,
        "items_done": s.items_done,
        "chunks_added": s.chunks_added,
        "embed_ok": getattr(s, "embed_ok", 0),
        "embed_failed": getattr(s, "embed_failed", 0),
        "embed_multimodal_ok": getattr(s, "embed_multimodal_ok", 0),
        "embed_multimodal_failed": getattr(s, "embed_multimodal_failed", 0),
        "embed_text_ok": getattr(s, "embed_text_ok", 0),
        "insert_ok": getattr(s, "insert_ok", 0),
        "insert_conflict": getattr(s, "insert_conflict", 0),
        "total_chunks": stats["total_chunks"],
        "pending": stats["pending"],
        "last_run": s.last_run.isoformat() if s.last_run else None,
        "types": s.types,
        "last_errors": s.errors[-5:],
    }


@router.post("/admin/media-embedder/start")
async def media_embedder_start(body: MediaStartBody, _uid: int = Depends(require_owner)) -> dict:
    mgr = get_media_embedder_manager()
    mgr.start(types=body.types)
    return {"status": "started", "types": mgr.status.types}


@router.post("/admin/media-embedder/stop")
async def media_embedder_stop(_uid: int = Depends(require_owner)) -> dict:
    mgr = get_media_embedder_manager()
    mgr.stop()
    return {"status": "stopped"}


@router.delete("/admin/media-embedder/chunks")
async def media_embedder_clear(_uid: int = Depends(require_owner)) -> dict:
    mgr = get_media_embedder_manager()
    deleted = await mgr.clear_chunks()
    return {"deleted": deleted}

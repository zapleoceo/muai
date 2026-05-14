import json
import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.services.embedder import get_embedder_status
from app.services.media_embedder import get_media_embedder_manager

from app.api.auth import require_owner
from app.services import stats as stats_svc

router = APIRouter()


@router.get("/admin/stats")
async def get_stats(_uid: int = Depends(require_owner)) -> dict:
    return await stats_svc.get_dashboard_stats()


@router.get("/admin/stream")
async def stream_dashboard(_uid: int = Depends(require_owner)) -> StreamingResponse:
    async def _event_iter():
        while True:
            stats = await stats_svc.get_dashboard_stats()

            async with AsyncSessionLocal() as session:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                chunk_stats = await MessageRepo(session).chunk_stats()
            embedder = {**get_embedder_status(), **chunk_stats}

            mgr = get_media_embedder_manager()
            media_stats = await mgr.get_stats()
            s = mgr.status
            media = {
                "running": s.running,
                "enabled": s.enabled,
                "current_item": s.current_item,
                "items_done": s.items_done,
                "chunks_added": s.chunks_added,
                "total_chunks": media_stats["total_chunks"],
                "pending": media_stats["pending"],
                "last_run": s.last_run.isoformat() if s.last_run else None,
                "types": s.types,
                "last_errors": s.errors[-5:],
            }

            payload = {"stats": stats, "embedder": embedder, "media_embedder": media}
            data = json.dumps(payload, ensure_ascii=False)
            yield f"data: {data}\n\n"
            await asyncio.sleep(10)

    return StreamingResponse(
        _event_iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

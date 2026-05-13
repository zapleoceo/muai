from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.api.auth import require_owner
from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.services.embedder import get_embedder_status, start_embedder, stop_embedder

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

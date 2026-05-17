"""Internal endpoints for VERA: RAG search + credential fetch."""
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.config import get_settings
from app.db.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter()


class _SearchRequest(BaseModel):
    query: str
    top_k: int = 5


def _auth(authorization: str | None = Header(default=None)) -> None:
    secret = get_settings().api_secret_key
    if not secret or authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/internal/rag/search")
async def rag_search(body: _SearchRequest, _: None = Depends(_auth)) -> dict:
    try:
        from app.llm.embedding import embed_text
        from app.db.database import AsyncSessionLocal
        from app.db.repository import MessageRepo

        q_vec = await embed_text(body.query, task_type="RETRIEVAL_QUERY")
        if q_vec is None:
            return {"chunks": [], "text": "Embedding service unavailable."}

        async with AsyncSessionLocal() as session:
            text_rows = await MessageRepo(session).search_chunks(q_vec, limit=body.top_k)

        chunks = []
        for r in text_rows:
            chunk_text = getattr(r, "chunk_text", "") or ""
            if chunk_text:
                chat_title = getattr(r, "chat_title", "") or ""
                chunks.append({"text": chunk_text, "chat": chat_title})

        combined = "\n\n".join(
            f"[{c['chat']}]\n{c['text']}" if c["chat"] else c["text"]
            for c in chunks
        )
        return {"chunks": chunks, "text": combined or "Ничего не найдено."}

    except Exception:
        logger.exception("RAG search failed")
        return {"chunks": [], "text": "Ошибка поиска."}


@router.get("/internal/vera-credentials/{cred_type}")
async def get_vera_credential(cred_type: str, _: None = Depends(_auth)) -> dict:
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text("SELECT data FROM vera_credentials WHERE type=:t AND is_active=true"),
            {"t": cred_type},
        )
        r = row.one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Credential not found")
    data = r.data if isinstance(r.data, dict) else json.loads(r.data)
    return {"data": data}

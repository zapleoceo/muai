from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.embedding import embed_text
from app.services.answering_types import PlanScope
from app.services.plan_executor.links import build_message_link


async def tool_rag_search(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_ids: list[int] | None,
    query: str,
    top_k: int,
) -> tuple[list[dict], dict]:
    q_vec = await embed_text(query, task_type="RETRIEVAL_QUERY")
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        text_rows = await MessageRepo(session).search_chunks(
            q_vec,
            limit=top_k,
            chat_id=chat_id if scope == PlanScope.CURRENT_CHAT else None,
            chat_ids=chat_ids if scope != PlanScope.CURRENT_CHAT else None,
        )
        media_rows = await MessageRepo(session).search_media_chunks(
            q_vec,
            limit=top_k,
            chat_id=chat_id if scope == PlanScope.CURRENT_CHAT else None,
            chat_ids=chat_ids if scope != PlanScope.CURRENT_CHAT else None,
        )

    candidates: list[dict] = []
    for r in text_rows:
        dist = float(getattr(r, "distance", 0.0) or 0.0)
        candidates.append({
            "kind": "text",
            "score": dist,
            "chunk_id": int(r.id),
            "chat_id": int(r.chat_id),
            "chat_title": r.chat_title,
            "text": r.chunk_text,
            "msg_date_from": r.msg_date_from.isoformat() if getattr(r, "msg_date_from", None) else None,
            "msg_date_to": r.msg_date_to.isoformat() if getattr(r, "msg_date_to", None) else None,
            "chat_username": getattr(r, "chat_username", None),
            "max_tg_msg_id": int(getattr(r, "max_tg_msg_id", 0) or 0) or None,
            "min_msg_id": int(getattr(r, "min_msg_id", 0) or 0) or None,
            "max_msg_id": int(getattr(r, "max_msg_id", 0) or 0) or None,
            "msg_count": int(getattr(r, "msg_count", 0) or 0) or None,
            "meta": getattr(r, "meta", None),
            "link": build_message_link(
                chat_id=int(r.chat_id),
                chat_type=None,
                chat_username=getattr(r, "chat_username", None),
                telegram_msg_id=int(getattr(r, "max_tg_msg_id", 0) or 0) or None,
            ),
        })

    for r in media_rows:
        dist = float(getattr(r, "distance", 0.0) or 0.0)
        source_tg_msg_id = int(getattr(r, "source_tg_msg_id", 0) or 0) or None
        candidates.append({
            "kind": "media",
            "score": dist,
            "chunk_id": -int(r.id),
            "chat_id": int(r.chat_id),
            "chat_title": getattr(r, "chat_title", None),
            "text": r.chunk_text,
            "msg_date_from": getattr(r, "date_utc", None).isoformat() if getattr(r, "date_utc", None) else None,
            "msg_date_to": getattr(r, "date_utc", None).isoformat() if getattr(r, "date_utc", None) else None,
            "chat_username": getattr(r, "chat_username", None),
            "max_tg_msg_id": source_tg_msg_id,
            "min_msg_id": None,
            "max_msg_id": None,
            "msg_count": 1,
            "meta": getattr(r, "meta", None),
            "link": build_message_link(
                chat_id=int(r.chat_id),
                chat_type=None,
                chat_username=getattr(r, "chat_username", None),
                telegram_msg_id=source_tg_msg_id,
            ),
        })

    candidates.sort(key=lambda x: float(x.get("score") or 0.0))
    items = candidates[: max(0, int(top_k))]
    return items, {"count": len(items), "top_k": top_k, "text_count": len(text_rows), "media_count": len(media_rows)}

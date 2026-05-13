from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.auth import require_owner
from app.services.interactions import list_interactions

router = APIRouter()

_VALID_FEEDBACK = {"like", "dislike", "all", "none"}


class InteractionOut(BaseModel):
    id: int
    created_at: str | None
    user_id: int | None
    chat_id: int
    query: str
    router_plan: dict | None
    tool_runs: list[dict] | None
    retrieved_summary: dict | None
    answer_text: str | None
    feedback: str | None
    feedback_at: str | None


@router.get("/admin/interactions", response_model=list[InteractionOut])
async def get_interactions(
    feedback: str = Query("all"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _uid: int = Depends(require_owner),
) -> list[InteractionOut]:
    if feedback not in _VALID_FEEDBACK:
        raise HTTPException(status_code=400, detail="Invalid feedback")
    f = None if feedback == "all" else None if feedback == "none" else feedback
    rows = await list_interactions(feedback=f, limit=limit, offset=offset)
    if feedback == "none":
        rows = [r for r in rows if r.feedback is None]
    return [
        InteractionOut(
            id=int(r.id),
            created_at=r.created_at.isoformat() if r.created_at else None,
            user_id=int(r.user_id) if r.user_id is not None else None,
            chat_id=int(r.chat_id),
            query=str(r.query),
            router_plan=r.router_plan,
            tool_runs=r.tool_runs,
            retrieved_summary=r.retrieved_summary,
            answer_text=(r.answer_text[:2000] if r.answer_text else None),
            feedback=r.feedback,
            feedback_at=r.feedback_at.isoformat() if r.feedback_at else None,
        )
        for r in rows
    ]

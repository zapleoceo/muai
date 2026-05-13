from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.auth import require_owner
from app.services.router_suggestions import list_router_suggestions, set_router_suggestion_status

router = APIRouter()

_VALID_STATUSES = {"new", "approved", "rejected", "applied", "all"}


class RouterSuggestionOut(BaseModel):
    id: int
    created_at: str | None
    reviewed_at: str | None
    status: str
    reviewer_user_id: int | None

    query: str
    current_plan: dict | None
    proposed_plan: dict | None
    proposed_rule: str | None
    context_summary: dict | None
    feedback: dict | None
    meta: dict | None


@router.get("/admin/router-suggestions", response_model=list[RouterSuggestionOut])
async def get_router_suggestions(
    status: str = Query("new"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _uid: int = Depends(require_owner),
) -> list[RouterSuggestionOut]:
    if status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    rows = await list_router_suggestions(status=status, limit=limit, offset=offset)
    return [
        RouterSuggestionOut(
            id=int(r.id),
            created_at=r.created_at.isoformat() if r.created_at else None,
            reviewed_at=r.reviewed_at.isoformat() if r.reviewed_at else None,
            status=str(r.status),
            reviewer_user_id=int(r.reviewer_user_id) if r.reviewer_user_id is not None else None,
            query=str(r.query),
            current_plan=r.current_plan,
            proposed_plan=r.proposed_plan,
            proposed_rule=r.proposed_rule,
            context_summary=r.context_summary,
            feedback=r.feedback,
            meta=r.meta,
        )
        for r in rows
    ]


@router.post("/admin/router-suggestions/{suggestion_id}/approve")
async def approve_router_suggestion(
    suggestion_id: int,
    uid: int = Depends(require_owner),
) -> dict:
    await set_router_suggestion_status(
        suggestion_id=suggestion_id,
        status="approved",
        reviewer_user_id=uid,
    )
    return {"ok": True}


@router.post("/admin/router-suggestions/{suggestion_id}/reject")
async def reject_router_suggestion(
    suggestion_id: int,
    uid: int = Depends(require_owner),
) -> dict:
    await set_router_suggestion_status(
        suggestion_id=suggestion_id,
        status="rejected",
        reviewer_user_id=uid,
    )
    return {"ok": True}

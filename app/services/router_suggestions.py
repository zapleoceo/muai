from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db.database import AsyncSessionLocal
from app.db.models import RouterSuggestion


async def list_router_suggestions(
    *,
    status: str | None,
    limit: int = 100,
    offset: int = 0,
) -> list[RouterSuggestion]:
    q = select(RouterSuggestion).order_by(RouterSuggestion.created_at.desc())
    if status and status != "all":
        q = q.where(RouterSuggestion.status == status)
    q = q.limit(limit).offset(offset)
    async with AsyncSessionLocal() as session:
        return list((await session.execute(q)).scalars().all())


async def set_router_suggestion_status(
    *,
    suggestion_id: int,
    status: str,
    reviewer_user_id: int,
) -> None:
    reviewed_at = datetime.now(tz=timezone.utc)
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(RouterSuggestion)
            .where(RouterSuggestion.id == suggestion_id)
            .values(status=status, reviewer_user_id=reviewer_user_id, reviewed_at=reviewed_at)
        )
        await session.commit()

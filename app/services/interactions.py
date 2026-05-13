from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db.database import AsyncSessionLocal
from app.db.models import Interaction


async def create_interaction(
    *,
    user_id: int | None,
    chat_id: int,
    query: str,
    router_plan: dict | None,
    router_raw: str | None,
    tool_runs: list[dict] | None,
    retrieved_summary: dict | None,
    answer_text: str | None,
) -> int:
    row = Interaction(
        user_id=user_id,
        chat_id=chat_id,
        query=query,
        router_plan=router_plan,
        router_raw=router_raw,
        tool_runs=tool_runs,
        retrieved_summary=retrieved_summary,
        answer_text=answer_text,
    )
    async with AsyncSessionLocal() as session:
        session.add(row)
        await session.flush()
        await session.commit()
        return int(row.id)


async def set_feedback(
    *,
    interaction_id: int,
    feedback: str,
    comment: str | None = None,
) -> None:
    now = datetime.now(tz=timezone.utc)
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Interaction)
            .where(Interaction.id == interaction_id)
            .values(feedback=feedback, feedback_comment=comment, feedback_at=now)
        )
        await session.commit()


async def list_interactions(
    *,
    feedback: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Interaction]:
    q = select(Interaction).order_by(Interaction.created_at.desc())
    if feedback and feedback != "all":
        q = q.where(Interaction.feedback == feedback)
    q = q.limit(limit).offset(offset)
    async with AsyncSessionLocal() as session:
        return list((await session.execute(q)).scalars().all())

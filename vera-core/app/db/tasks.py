from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Task


async def save_task(source: str, user_id: int | None, input_text: str) -> int:
    async with get_session() as session:
        task = Task(
            source=source,
            user_id=user_id,
            input_text=input_text,
            status="pending",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def update_task(
    task_id: int,
    result: str,
    score: float,
    attempts: int,
    agents_used: list[str],
    duration_ms: int,
) -> None:
    async with get_session() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return
        task.final_result = result
        task.quality_score = score
        task.attempts = attempts
        task.agents_used = agents_used
        task.duration_ms = duration_ms
        task.status = "done"
        await session.commit()

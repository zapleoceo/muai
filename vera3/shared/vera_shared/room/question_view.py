"""Чтение вопросов владельцу и ответов к ним (история хранится целиком)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_room import RoomTaskAnswerRow, RoomTaskQuestionRow


def question_dict(q: RoomTaskQuestionRow, answers: list[RoomTaskAnswerRow]) -> dict[str, Any]:
    return {"qid": q.qid, "room": q.room, "task_id": q.task_id, "asked_by": q.asked_by,
            "question": q.question, "asked_at": q.asked_at.isoformat(), "status": q.status,
            "acked_at": q.acked_at.isoformat() if q.acked_at else None, "ack_by": q.ack_by,
            "answers": [{"text": a.text, "by": a.answered_by,
                         "at": a.answered_at.isoformat()} for a in answers]}


async def load_questions(s: AsyncSession, room: str, task_id: str,
                         ) -> list[tuple[RoomTaskQuestionRow, list[RoomTaskAnswerRow]]]:
    qs = (await s.execute(select(RoomTaskQuestionRow).where(
        RoomTaskQuestionRow.room == room, RoomTaskQuestionRow.task_id == task_id,
    ).order_by(RoomTaskQuestionRow.qid))).scalars().all()
    if not qs:
        return []
    answers = (await s.execute(select(RoomTaskAnswerRow).where(
        RoomTaskAnswerRow.qid.in_([q.qid for q in qs])).order_by(RoomTaskAnswerRow.id))
    ).scalars().all()
    return [(q, [a for a in answers if a.qid == q.qid]) for q in qs]


async def list_questions(s: AsyncSession, room: str, task_id: str) -> list[dict[str, Any]]:
    return [question_dict(q, a) for q, a in await load_questions(s, room, task_id)]

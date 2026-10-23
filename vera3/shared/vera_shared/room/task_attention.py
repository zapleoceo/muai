"""Внимание для набора задач: подтягивает вопросы, паузы и время захвата одним заходом."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_room import (
    RoomTaskAnswerRow,
    RoomTaskEventRow,
    RoomTaskQuestionRow,
    RoomTaskRow,
)
from vera_shared.room.attention import Attention, attention, attention_dict
from vera_shared.room.owner_block import BY_OWNER, fold_pauses
from vera_shared.room.task_view import task_dict


async def attention_map(s: AsyncSession, rows: list[RoomTaskRow],
                        now: datetime) -> dict[tuple[str, str], Attention]:
    if not rows:
        return {}
    ids = [r.task_id for r in rows]
    rooms = {r.room for r in rows}
    asked_rows = (await s.execute(
        select(RoomTaskQuestionRow.room, RoomTaskQuestionRow.task_id,
               func.min(RoomTaskQuestionRow.asked_at))
        .where(RoomTaskQuestionRow.room.in_(rooms), RoomTaskQuestionRow.task_id.in_(ids),
               RoomTaskQuestionRow.status == "open")
        .group_by(RoomTaskQuestionRow.room, RoomTaskQuestionRow.task_id))).all()
    asked = {(room, tid): at for room, tid, at in asked_rows}
    answered_rows = (await s.execute(
        select(RoomTaskQuestionRow.room, RoomTaskQuestionRow.task_id,
               func.min(RoomTaskAnswerRow.answered_at))
        .join(RoomTaskAnswerRow, RoomTaskAnswerRow.qid == RoomTaskQuestionRow.qid)
        .where(RoomTaskQuestionRow.room.in_(rooms), RoomTaskQuestionRow.task_id.in_(ids),
               RoomTaskQuestionRow.status == "answered")
        .group_by(RoomTaskQuestionRow.room, RoomTaskQuestionRow.task_id))).all()
    answered = {(room, tid): at for room, tid, at in answered_rows}
    events = (await s.execute(
        select(RoomTaskEventRow.room, RoomTaskEventRow.task_id, RoomTaskEventRow.kind,
               RoomTaskEventRow.at, RoomTaskEventRow.data)
        .where(RoomTaskEventRow.room.in_(rooms), RoomTaskEventRow.task_id.in_(ids),
               RoomTaskEventRow.kind.in_(("paused", "resumed", "claimed")))
        .order_by(RoomTaskEventRow.id))).all()
    pauses: dict[tuple[str, str], list[tuple[str, dict[str, Any] | None]]] = {}
    claimed: dict[tuple[str, str], datetime] = {}
    for room, tid, kind, at, data in events:
        if kind == "claimed":
            claimed[(room, tid)] = at
        else:
            pauses.setdefault((room, tid), []).append((kind, data))
    folded = {key: fold_pauses(evs) for key, evs in pauses.items()}
    paused = {key: any(st.values()) for key, st in folded.items()}
    return {(r.room, r.task_id): attention(
        r, now, open_question_at=asked.get((r.room, r.task_id)),
        paused=paused.get((r.room, r.task_id), False),
        owner_paused=folded.get((r.room, r.task_id), {}).get(BY_OWNER, False),
        claimed_at=claimed.get((r.room, r.task_id)),
        answered_at=answered.get((r.room, r.task_id))) for r in rows}


async def tasks_with_attention(s: AsyncSession, rows: list[RoomTaskRow],
                               now: datetime) -> list[dict[str, Any]]:
    amap = await attention_map(s, rows, now)
    return [{**task_dict(r), "attention": attention_dict(amap[(r.room, r.task_id)])}
            for r in rows]

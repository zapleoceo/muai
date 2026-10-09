"""Внимание для набора задач: подтягивает вопросы, паузы и время захвата одним заходом."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room.attention import Attention, attention, attention_dict
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
    events = (await s.execute(
        select(RoomTaskEventRow.room, RoomTaskEventRow.task_id, RoomTaskEventRow.kind,
               RoomTaskEventRow.at)
        .where(RoomTaskEventRow.room.in_(rooms), RoomTaskEventRow.task_id.in_(ids),
               RoomTaskEventRow.kind.in_(("paused", "resumed", "claimed")))
        .order_by(RoomTaskEventRow.id))).all()
    paused: dict[tuple[str, str], bool] = {}
    claimed: dict[tuple[str, str], datetime] = {}
    for room, tid, kind, at in events:
        if kind == "claimed":
            claimed[(room, tid)] = at
        else:
            paused[(room, tid)] = kind == "paused"
    return {(r.room, r.task_id): attention(
        r, now, open_question_at=asked.get((r.room, r.task_id)),
        paused=paused.get((r.room, r.task_id), False),
        claimed_at=claimed.get((r.room, r.task_id))) for r in rows}


async def tasks_with_attention(s: AsyncSession, rows: list[RoomTaskRow],
                               now: datetime) -> list[dict[str, Any]]:
    amap = await attention_map(s, rows, now)
    return [{**task_dict(r), "attention": attention_dict(amap[(r.room, r.task_id)])}
            for r in rows]

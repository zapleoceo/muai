"""Вкладки `/tasks`: раскладка задач по attention, той же функцией, что у MCP."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow
from vera_shared.room import questions
from vera_shared.room.attention import (
    CANCELLED,
    DONE,
    LEASE_EXPIRED,
    NEEDS_OWNER,
    STALE_PROGRESS,
    Attention,
)
from vera_shared.room.intervals import Segment, build_segments
from vera_shared.room.question_view import load_questions
from vera_shared.room.task_attention import attention_map
from vera_shared.timeutil import utc_naive_now

from dashboard import tasks_repo
from dashboard.tasks_gantt import GanttRow
from dashboard.tasks_questions_view import Questions

TABS = ("work", "me", "done")
ME_STATES = (NEEDS_OWNER, LEASE_EXPIRED, STALE_PROGRESS)
_FAR_PAST = datetime(1970, 1, 1)


@dataclass(frozen=True)
class TaskItem:
    row: RoomTaskRow
    attention: Attention


def classify(att: Attention) -> str:
    if att.state in (DONE, CANCELLED):
        return "done"
    return "me" if att.state in ME_STATES else "work"


def _sort_key(item: TaskItem) -> tuple[int, float]:
    last = item.row.last_progress_at or item.row.updated_at or _FAR_PAST
    return (item.row.priority, -last.timestamp())


def split_tabs(items: list[TaskItem]) -> dict[str, list[TaskItem]]:
    tabs: dict[str, list[TaskItem]] = {t: [] for t in TABS}
    for it in items:
        tabs[classify(it.attention)].append(it)
    for lst in tabs.values():
        lst.sort(key=_sort_key)
    return tabs


async def load_tabs(now: datetime | None = None) -> dict[str, list[TaskItem]]:
    now = now or utc_naive_now()
    async with get_session() as s:
        rows = await tasks_repo.all_tasks(s)
        amap = await attention_map(s, rows, now)
    return split_tabs([TaskItem(r, amap[(r.room, r.task_id)]) for r in rows])


async def load_detail(room: str, task_id: str, now: datetime | None = None,
                      ) -> tuple[TaskItem, list[RoomTaskEventRow], Questions] | None:
    now = now or utc_naive_now()
    async with get_session() as s:
        row = await tasks_repo.one_task(s, room, task_id)
        if row is None:
            return None
        amap = await attention_map(s, [row], now)
        events = await tasks_repo.task_events(s, room, task_id)
        qs = await load_questions(s, room, task_id)
    return TaskItem(row, amap[(row.room, row.task_id)]), events, qs


async def submit_answer(room: str, task_id: str, qid: int, text: str) -> None:
    await questions.answer(room=room, task_id=task_id, qid=qid, text=text)


def plan_of(row: RoomTaskRow) -> tuple[datetime, datetime] | None:
    if row.plan_start and row.plan_end and row.plan_end > row.plan_start:
        return (row.plan_start, row.plan_end)
    return None


def segments_of(row: RoomTaskRow, events: list[RoomTaskEventRow], now: datetime) -> list[Segment]:
    return build_segments(events, now=now, lease_until=row.lease_until)


async def load_gantt_rows(items: list[TaskItem], now: datetime) -> list[GanttRow]:
    async with get_session() as s:
        events = await tasks_repo.events_of_tasks(
            s, [(i.row.room, i.row.task_id) for i in items])
    by_task: dict[tuple[str, str], list[RoomTaskEventRow]] = {}
    for e in events:
        by_task.setdefault((e.room, e.task_id), []).append(e)
    return [GanttRow(i.row.title or i.row.task_id,
                     segments_of(i.row, by_task.get((i.row.room, i.row.task_id), []), now),
                     plan_of(i.row)) for i in items]

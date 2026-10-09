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
# Личный блок /tasks; всё остальное — «Рабочие проекты». Сравнение без учёта регистра.
PERSONAL_PROJECTS = frozenset({"личное", "личные проекты", "личные интеграции", "устройства",
                               "vera", "sniffer"})
NO_PROJECT = "Без проекта"
WORK_BLOCK = "Рабочие проекты"
PERSONAL_BLOCK = "Личное и Vera"


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


@dataclass(frozen=True)
class Group:
    name: str
    items: list[TaskItem]
    attention: int


@dataclass(frozen=True)
class Block:
    title: str
    groups: list[Group]

    @property
    def count(self) -> int:
        return sum(len(g.items) for g in self.groups)


def project_name(item: TaskItem) -> str:
    return (item.row.project or "").strip() or NO_PROJECT


def group_tasks(items: list[TaskItem]) -> list[Block]:
    """Два блока, в них группы по проекту: больше задач «нужен я», затем имя; внутри — порядок входа."""
    by_key: dict[str, list[TaskItem]] = {}
    names: dict[str, str] = {}
    for it in items:
        name = project_name(it)
        by_key.setdefault(name.casefold(), []).append(it)
        names.setdefault(name.casefold(), name)
    work: list[Group] = []
    personal: list[Group] = []
    for key, lst in by_key.items():
        g = Group(names[key], lst, sum(1 for i in lst if i.attention.state in ME_STATES))
        (personal if key in PERSONAL_PROJECTS else work).append(g)
    nokey = NO_PROJECT.casefold()
    work.sort(key=lambda g: (g.name.casefold() == nokey, -g.attention, g.name.casefold()))
    personal.sort(key=lambda g: (-g.attention, g.name.casefold()))
    return [b for b in (Block(WORK_BLOCK, work), Block(PERSONAL_BLOCK, personal)) if b.groups]


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
    ordered = [(g.name, i) for b in group_tasks(items) for g in b.groups for i in g.items]
    return [GanttRow(i.row.title or i.row.task_id,
                     segments_of(i.row, by_task.get((i.row.room, i.row.task_id), []), now),
                     plan_of(i.row), name) for name, i in ordered]

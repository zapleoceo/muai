"""SQL почасового отчёта Office: всё, что `load_report` читает из комнаты, одной сессией."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import (
    RoomMessageRow,
    RoomTaskEventRow,
    RoomTaskQuestionRow,
    RoomTaskRow,
)

from dashboard.office_card_queries import load_transitions
from dashboard.office_scope_record import SCOPE_PREFIX

_CONTROL_KINDS = ("paused", "review", "resumed", "claimed", "handoff_accept", "unblocked",
                  "released", "blocked", "waiting", "ack_answer")


@dataclass(frozen=True)
class HourlyInputs:
    rows: list[RoomTaskRow]
    questions: list[RoomTaskQuestionRow]
    events: list[RoomTaskEventRow]
    controls: list[RoomTaskEventRow]
    control_next: list[RoomTaskEventRow]
    runtime_rows: list[RoomMessageRow]
    scope_rows: list[RoomMessageRow]
    transitions: list[RoomTaskEventRow]


async def load_hourly_inputs(now: datetime, window: timedelta) -> HourlyInputs:
    ev = RoomTaskEventRow
    valid_result = or_(ev.kind == "done", and_(ev.kind == "progress", ev.text.is_not(None),
                                                ev.text != "",
                                                ev.data.has_key("next_checkpoint_seconds")))  # noqa: W601
    latest_ranked = select(ev.id.label("id"), func.row_number().over(
        partition_by=(ev.room, ev.task_id), order_by=(ev.at.desc(), ev.id.desc())
    ).label("rank")).where(valid_result, ev.at <= now).subquery()
    control_ranked = select(ev.id.label("id"), func.row_number().over(
        partition_by=(ev.room, ev.task_id), order_by=(ev.at.desc(), ev.id.desc())
    ).label("rank")).where(ev.kind.in_(_CONTROL_KINDS), ev.at <= now).subquery()
    async with get_session() as s:
        rows_result = await s.execute(select(RoomTaskRow))
        questions_result = await s.execute(select(RoomTaskQuestionRow).where(
            RoomTaskQuestionRow.status.in_(("open", "answered"))))
        events_result = await s.execute(select(ev).where(
            valid_result, ev.at <= now,
            or_(ev.id.in_(select(latest_ranked.c.id).where(latest_ranked.c.rank == 1)),
                ev.at >= now - window)
        ))
        controls_result = await s.execute(select(ev).where(
            ev.id.in_(select(control_ranked.c.id).where(control_ranked.c.rank == 1))))
        runtime_result = await s.execute(select(RoomMessageRow).where(
            RoomMessageRow.room == "main", RoomMessageRow.from_agent == "dot",
            RoomMessageRow.to_agent == "office-dashboard",
            RoomMessageRow.task_id.is_(None), RoomMessageRow.status == "info",
            RoomMessageRow.body.startswith("OFFICE_RUNTIME_V1\n"),
            RoomMessageRow.created_at <= now,
        ).order_by(RoomMessageRow.id.desc()).limit(20))
        scope_result = await s.execute(select(RoomMessageRow).where(
            RoomMessageRow.room == "main", RoomMessageRow.from_agent == "dot",
            RoomMessageRow.to_agent == "office-dashboard",
            RoomMessageRow.task_id.is_(None), RoomMessageRow.status == "info",
            RoomMessageRow.body.startswith(SCOPE_PREFIX),
            RoomMessageRow.created_at <= now,
        ).order_by(RoomMessageRow.id.desc()).limit(1))
        rows = list(rows_result.scalars().all())
        questions = list(questions_result.scalars().all())
        events = list(events_result.scalars().all())
        controls = list(controls_result.scalars().all())
        blocked = [c for c in controls if c.kind == "blocked"]
        control_next: list[RoomTaskEventRow] = []
        if blocked:
            following = select(ev.id.label("id"), func.row_number().over(
                partition_by=(ev.room, ev.task_id), order_by=ev.id.asc()
            ).label("rank")).where(or_(*[
                and_(ev.room == c.room, ev.task_id == c.task_id, ev.id > c.id)
                for c in blocked]), ev.at <= now).subquery()
            next_result = await s.execute(select(ev).where(
                ev.id.in_(select(following.c.id).where(following.c.rank == 1))))
            control_next = list(next_result.scalars().all())
        runtime_rows = list(runtime_result.scalars().all())
        scope_rows = list(scope_result.scalars().all())
        transitions = await load_transitions(s, now, window)
    return HourlyInputs(rows, questions, events, controls, control_next,
                        runtime_rows, scope_rows, transitions)


async def load_recent_task_events(room: str, task_id: str) -> list[RoomTaskEventRow]:
    """Newest history for the Office modal; the generic task page loads oldest first."""
    async with get_session() as s:
        result = await s.execute(select(RoomTaskEventRow).where(
            RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id
        ).order_by(RoomTaskEventRow.id.desc()).limit(200))
        return list(result.scalars().all())

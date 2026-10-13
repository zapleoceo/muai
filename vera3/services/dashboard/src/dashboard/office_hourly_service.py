"""Owner dashboard projection of Room cards and timestamped results."""
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
from vera_shared.room.attention import (
    ANSWERED,
    PAUSED,
    Attention,
    attention,
)
from vera_shared.timeutil import utc_naive_now

from dashboard.office_runtime_service import RuntimeSnapshot, latest_runtime_snapshot
from dashboard.office_status import (
    CANCELLED,
    DONE,
    STATUSES,
    StatusInfo,
    bound_reason_of,
    describe_status,
)

WINDOW = timedelta(hours=1)
REMINDER = timedelta(hours=6)
STATUS_LABELS = STATUSES
VISIBLE_STATUSES = tuple(s for s in STATUSES if s not in (DONE, CANCELLED))
RESULT_KINDS = ("progress", "done")


@dataclass(frozen=True)
class HourlyCard:
    row: RoomTaskRow
    status: str
    info: StatusInfo
    attention: Attention
    question: RoomTaskQuestionRow | None
    last_confirmed: RoomTaskEventRow | None
    evidence: str
    runtime_mapping: str
    coordination_note: str


@dataclass(frozen=True)
class HourlyReport:
    as_of: datetime
    window_start: datetime
    cards: list[HourlyCard]
    results: list[RoomTaskEventRow]
    counts: dict[str, int]
    runtime: RuntimeSnapshot | None = None


def status_info_of(row: RoomTaskRow, att: Attention, question: RoomTaskQuestionRow | None,
                   now: datetime, control: str | None = None,
                   event_at: datetime | None = None,
                   control_event: RoomTaskEventRow | None = None,
                   control_next: RoomTaskEventRow | None = None) -> StatusInfo:
    return describe_status(
        row, now, has_open_question=question is not None, answered=att.state == ANSWERED,
        control="paused" if att.state == PAUSED else control, event_at=event_at,
        control_text=control_event.text if control_event else None,
        control_agent=control_event.agent if control_event else None,
        control_data=control_event.data if control_event else None,
        control_bound_reason=bound_reason_of(control_event, control_next))


def status_of(row: RoomTaskRow, att: Attention, question: RoomTaskQuestionRow | None,
              now: datetime, control: str | None = None) -> str:
    return status_info_of(row, att, question, now, control).status


def evidence_of(event: RoomTaskEventRow | None, now: datetime) -> str:
    if event is None:
        return "Сообщения о результате в Room нет; внешнее подтверждение неизвестно"
    age = now - event.at
    if age > timedelta(hours=24):
        return "Сообщение о результате старше суток; внешнее подтверждение неизвестно"
    return "Сообщение о результате с отметкой времени; внешнее подтверждение неизвестно"


def next_question_reminder(question: RoomTaskQuestionRow, now: datetime) -> datetime:
    """Next six-hour display checkpoint; an answered question is never passed here."""
    elapsed = max((now - question.asked_at) // REMINDER, 0)
    return question.asked_at + (elapsed + 1) * REMINDER


def build_report(rows: list[RoomTaskRow], attentions: dict[tuple[str, str], Attention],
                 questions: list[RoomTaskQuestionRow], events: list[RoomTaskEventRow],
                 now: datetime, controls: list[RoomTaskEventRow] | None = None,
                 control_next: list[RoomTaskEventRow] | None = None) -> HourlyReport:
    open_questions = {(q.room, q.task_id): q for q in questions if q.status == "open"}
    control_events = {(e.room, e.task_id): e for e in (controls or [])}
    control_by_task = {k: e.kind for k, e in control_events.items()}
    next_events = {(e.room, e.task_id): e for e in (control_next or [])}
    latest: dict[tuple[str, str], RoomTaskEventRow] = {}
    results: list[RoomTaskEventRow] = []
    start = now - WINDOW
    for event in events:
        if (event.kind not in RESULT_KINDS or
                (event.kind == "progress" and
                 (not event.text or "next_checkpoint_seconds" not in (event.data or {})))):
            continue
        key = (event.room, event.task_id)
        if key not in latest or (event.at, event.id) > (latest[key].at, latest[key].id):
            latest[key] = event
        if start <= event.at <= now:
            results.append(event)
    cards: list[HourlyCard] = []
    counts = dict.fromkeys(VISIBLE_STATUSES, 0)
    for row in rows:
        key = (row.room, row.task_id)
        att = attentions[key]
        question = open_questions.get(key)
        control = control_by_task.get(key)
        last = latest.get(key)
        info = status_info_of(row, att, question, now, control, last.at if last else None,
                              control_events.get(key), next_events.get(key))
        status = info.status
        if status in counts:
            counts[status] += 1
            cards.append(HourlyCard(row, status, info, att, question, last,
                                    evidence_of(last, now), "Не установлена",
                                    "Пауза зафиксирована" if control == "paused" else ""))
    cards.sort(key=lambda c: (STATUS_LABELS.index(c.status), c.row.priority,
                              (c.row.project or "").casefold(), c.row.task_id))
    visible = {(c.row.room, c.row.task_id) for c in cards}
    results = [e for e in results if (e.room, e.task_id) in visible]
    results.sort(key=lambda e: (e.at, e.id), reverse=True)
    return HourlyReport(now, start, cards, results, counts)


async def load_report(now: datetime | None = None) -> HourlyReport:
    now = now or utc_naive_now()
    ev = RoomTaskEventRow
    valid_result = or_(ev.kind == "done", and_(ev.kind == "progress", ev.text.is_not(None),
                                                ev.text != "",
                                                ev.data.has_key("next_checkpoint_seconds")))  # noqa: W601
    latest_ranked = select(ev.id.label("id"), func.row_number().over(
        partition_by=(ev.room, ev.task_id), order_by=(ev.at.desc(), ev.id.desc())
    ).label("rank")).where(valid_result, ev.at <= now).subquery()
    control_ranked = select(ev.id.label("id"), func.row_number().over(
        partition_by=(ev.room, ev.task_id), order_by=(ev.at.desc(), ev.id.desc())
    ).label("rank")).where(ev.kind.in_(("paused", "review", "resumed", "claimed",
                                        "handoff_accept", "unblocked", "released",
                                        "blocked", "waiting", "ack_answer")),
                           ev.at <= now).subquery()
    async with get_session() as s:
        rows_result = await s.execute(select(RoomTaskRow))
        questions_result = await s.execute(select(RoomTaskQuestionRow).where(
            RoomTaskQuestionRow.status.in_(("open", "answered"))))
        events_result = await s.execute(select(ev).where(
            valid_result, ev.at <= now,
            or_(ev.id.in_(select(latest_ranked.c.id).where(latest_ranked.c.rank == 1)),
                ev.at >= now - WINDOW)
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
    controls_by_task = {(e.room, e.task_id): e.kind for e in controls}
    open_by_task = {(q.room, q.task_id): q for q in questions if q.status == "open"}
    answered = {(q.room, q.task_id) for q in questions if q.status == "answered"}
    attentions = {}
    for row in rows:
        key = (row.room, row.task_id)
        q = open_by_task.get(key)
        attentions[key] = (Attention(ANSWERED, "", None, None, None) if key in answered and not q
                           else attention(row, now, open_question_at=q.asked_at if q else None,
                                          paused=controls_by_task.get(key) == "paused"))
    report = build_report(rows, attentions, questions, events, now, controls, control_next)
    runtime = latest_runtime_snapshot(runtime_rows, now)
    return HourlyReport(report.as_of, report.window_start, report.cards, report.results,
                        report.counts, runtime)

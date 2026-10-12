"""Owner dashboard projection of Room cards and timestamped results."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import (
    RoomTaskEventRow,
    RoomTaskQuestionRow,
    RoomTaskRow,
)
from vera_shared.room.attention import (
    ANSWERED,
    NEEDS_OWNER,
    PAUSED,
    Attention,
    attention,
)
from vera_shared.timeutil import utc_naive_now

WINDOW = timedelta(hours=1)
REMINDER = timedelta(hours=6)
STATUS_LABELS = ("Новая / в очереди", "В работе", "Блокер", "На проверке", "Готово",
                 "Отменена")
RESULT_KINDS = ("progress", "done")


@dataclass(frozen=True)
class HourlyCard:
    row: RoomTaskRow
    status: str
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
    done_hidden: int


def status_of(row: RoomTaskRow, att: Attention, question: RoomTaskQuestionRow | None,
              now: datetime, control: str | None = None) -> str:
    if row.status == "done":
        return "Готово"
    if row.status == "cancelled":
        return "Отменена"
    if question is not None or att.state == NEEDS_OWNER:
        return "Блокер"
    if att.state == ANSWERED or control == "review":
        return "На проверке"
    if att.state == PAUSED or control == "paused":
        return "Новая / в очереди"
    if row.status == "blocked":
        return "Блокер"
    if row.lease_holder and row.lease_until and row.lease_until > now:
        return "В работе"
    return "Новая / в очереди"


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
                 now: datetime, controls: list[RoomTaskEventRow] | None = None) -> HourlyReport:
    open_questions = {(q.room, q.task_id): q for q in questions if q.status == "open"}
    control_by_task = {(e.room, e.task_id): e.kind for e in (controls or [])}
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
    counts = dict.fromkeys(STATUS_LABELS, 0)
    for row in rows:
        key = (row.room, row.task_id)
        att = attentions[key]
        question = open_questions.get(key)
        control = control_by_task.get(key)
        status = status_of(row, att, question, now, control)
        counts[status] += 1
        if status != "Готово":
            last = latest.get(key)
            cards.append(HourlyCard(row, status, att, question, last,
                                    evidence_of(last, now), "Не установлена",
                                    "Пауза зафиксирована" if control == "paused" else ""))
    cards.sort(key=lambda c: (STATUS_LABELS.index(c.status), c.row.priority,
                              (c.row.project or "").casefold(), c.row.task_id))
    visible = {(c.row.room, c.row.task_id) for c in cards if c.status != "Отменена"}
    results = [e for e in results if (e.room, e.task_id) in visible]
    results.sort(key=lambda e: (e.at, e.id), reverse=True)
    return HourlyReport(now, start, cards, results, counts, counts["Готово"])


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
                                        "blocked", "ack_answer")),
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
        rows = list(rows_result.scalars().all())
        questions = list(questions_result.scalars().all())
        events = list(events_result.scalars().all())
        controls = list(controls_result.scalars().all())
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
    return build_report(rows, attentions, questions, events, now, controls)

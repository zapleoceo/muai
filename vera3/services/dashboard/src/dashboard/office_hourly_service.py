"""Owner dashboard projection of Room cards and timestamped results."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import (
    RoomMessageRow,
    RoomTaskEventRow,
    RoomTaskQuestionRow,
    RoomTaskRow,
)
from vera_shared.room.attention import ANSWERED, Attention, attention
from vera_shared.timeutil import utc_naive_now

from dashboard.office_card_details import CardDetails, details_by_task
from dashboard.office_card_queries import load_transitions
from dashboard.office_hourly_status import (  # noqa: F401  re-exported public names
    evidence_of,
    next_question_reminder,
    status_info_of,
    status_of,
)
from dashboard.office_runtime_service import RuntimeSnapshot, latest_runtime_snapshot
from dashboard.office_scope import (
    ScopeConfig,
    apply_scope,
    env_config,
    mask_hidden_deps,
    scope_runtime,
)
from dashboard.office_status import (
    CANCELLED,
    DONE,
    STATUSES,
    StatusInfo,
)

WINDOW = timedelta(hours=1)
STATUS_LABELS = STATUSES
VISIBLE_STATUSES = tuple(s for s in STATUSES if s not in (DONE, CANCELLED))
RESULT_KINDS = ("progress", "done")
SCOPE_PREFIX = "OFFICE_SCOPE_V1\n"


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
    details: CardDetails | None = None


@dataclass(frozen=True)
class HourlyReport:
    as_of: datetime
    window_start: datetime
    cards: list[HourlyCard]
    results: list[RoomTaskEventRow]
    counts: dict[str, int]
    runtime: RuntimeSnapshot | None = None
    hidden_excluded: int = 0
    hidden_inactive: int = 0
    hidden_runtime: int = 0
    active_only_label: str = ""
    pending_cancellations: list[tuple[RoomTaskRow, str]] | None = None
    scope_message_id: str | None = None


def parse_scope_decisions(row: RoomMessageRow) -> dict[tuple[str, str], str] | None:
    """Read a dot-attributed scope record, never treating it as a Room status change."""
    if (row.room != "main" or row.from_agent != "dot" or
            row.to_agent != "office-dashboard" or row.task_id is not None or
            row.status != "info" or not row.body.startswith(SCOPE_PREFIX) or
            len(row.body) > 12000):
        return None
    try:
        raw: Any = json.loads(row.body[len(SCOPE_PREFIX):])
        entries = raw["pending_owner_cancellations"]
        if not isinstance(entries, list) or len(entries) > 100:
            return None
        out: dict[tuple[str, str], str] = {}
        for entry in entries:
            room, task_id, source = entry["room"], entry["task_id"], entry["source"]
            if (room != "main" or not isinstance(task_id, str) or
                    not 1 <= len(task_id) <= 128 or not isinstance(source, str) or
                    not source.startswith("room:") or not source[5:].isdigit() or
                    (room, task_id) in out):
                return None
            out[(room, task_id)] = source
        return out
    except (KeyError, TypeError, ValueError):
        return None


def build_report(rows: list[RoomTaskRow], attentions: dict[tuple[str, str], Attention],
                 questions: list[RoomTaskQuestionRow], events: list[RoomTaskEventRow],
                 now: datetime, controls: list[RoomTaskEventRow] | None = None,
                 control_next: list[RoomTaskEventRow] | None = None,
                 transitions: list[RoomTaskEventRow] | None = None,
                 scope: ScopeConfig | None = None,
                 include_closed: bool = False,
                 pending_decisions: dict[tuple[str, str], str] | None = None,
                 scope_message_id: str | None = None) -> HourlyReport:
    scope = scope or env_config()
    control_events = {(e.room, e.task_id): e for e in (controls or [])}
    control_by_task = {k: e.kind for k, e in control_events.items()}
    pending_decisions = pending_decisions or {}
    pending = [(r, pending_decisions[(r.room, r.task_id)]) for r in rows
               if (r.room, r.task_id) in pending_decisions
               and r.status not in ("done", "cancelled")]
    pending_keys = {(r.room, r.task_id) for r, _ in pending}
    split = apply_scope([r for r in rows if (r.room, r.task_id) not in pending_keys],
                        now, scope, control_by_task)
    open_questions = {(q.room, q.task_id): q for q in questions if q.status == "open"}
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
    counts = dict.fromkeys(STATUSES if include_closed else VISIBLE_STATUSES, 0)
    infos: dict[tuple[str, str], StatusInfo] = {}
    visible_pending = []
    for row in split.visible:
        key = (row.room, row.task_id)
        att = attentions[key]
        question = open_questions.get(key)
        control = control_by_task.get(key)
        last = latest.get(key)
        info = status_info_of(row, att, question, now, control, last.at if last else None,
                              control_events.get(key), next_events.get(key))
        status = info.status
        info = mask_hidden_deps(row, info, split.hidden_keys)
        infos[key] = info
        if status in counts:
            counts[status] += 1
            visible_pending.append((row, status, info, att, question, last, control))
    all_details = details_by_task([(p[0], p[5]) for p in visible_pending], infos, now,
                                  transitions or [], results, split.hidden_keys)
    for row, status, info, att, question, last, control in visible_pending:
        key = (row.room, row.task_id)
        details = all_details[key]
        cards.append(HourlyCard(row, status, info, att, question, last,
                                evidence_of(last, now), "Не установлена",
                                "Пауза зафиксирована" if control == "paused" else "", details))
    cards.sort(key=lambda c: (STATUS_LABELS.index(c.status), c.row.priority,
                              (c.row.project or "").casefold(), c.row.task_id))
    visible = {(c.row.room, c.row.task_id) for c in cards}
    results = [e for e in results if (e.room, e.task_id) in visible]
    results.sort(key=lambda e: (e.at, e.id), reverse=True)
    return HourlyReport(now, start, cards, results, counts, None,
                        split.hidden_excluded, split.hidden_inactive, 0,
                        "/".join(scope.active_only_names), pending, scope_message_id)


async def load_report(now: datetime | None = None,
                      include_closed: bool = False) -> HourlyReport:
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
        scope_result = await s.execute(select(RoomMessageRow).where(
            RoomMessageRow.room == "main", RoomMessageRow.from_agent == "dot",
            RoomMessageRow.to_agent == "office-dashboard",
            RoomMessageRow.task_id.is_(None), RoomMessageRow.status == "info",
            RoomMessageRow.body.startswith(SCOPE_PREFIX),
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
        scope_rows = list(scope_result.scalars().all())
        transitions = await load_transitions(s, now, WINDOW)
    scope_record = next(((r, parsed) for r in scope_rows
                         if (parsed := parse_scope_decisions(r)) is not None), None)
    pending_decisions = scope_record[1] if scope_record else {}
    scope_message_id = scope_record[0].message_id if scope_record else None
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
    report = build_report(rows, attentions, questions, events, now, controls, control_next,
                          transitions, include_closed=include_closed,
                          pending_decisions=pending_decisions,
                          scope_message_id=scope_message_id)
    runtime, hidden_runtime = scope_runtime(latest_runtime_snapshot(runtime_rows, now))
    return replace(report, runtime=runtime, hidden_runtime=hidden_runtime)


async def load_recent_task_events(room: str, task_id: str) -> list[RoomTaskEventRow]:
    """Newest history for the Office modal; the generic task page loads oldest first."""
    async with get_session() as s:
        result = await s.execute(select(RoomTaskEventRow).where(
            RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id
        ).order_by(RoomTaskEventRow.id.desc()).limit(200))
        return list(result.scalars().all())

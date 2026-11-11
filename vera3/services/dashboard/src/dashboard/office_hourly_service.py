"""Owner dashboard projection of Room cards and timestamped results.

Queries live in `office_hourly_repo`, the dot scope record parser in
`office_scope_record`; both stay importable from here."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from vera_shared.db.models_room import (
    RoomTaskEventRow,
    RoomTaskQuestionRow,
    RoomTaskRow,
)
from vera_shared.room.attention import ANSWERED, Attention, attention
from vera_shared.timeutil import utc_naive_now

from dashboard.office_card_details import CardDetails, details_by_task
from dashboard.office_hourly_repo import load_hourly_inputs, load_recent_task_events
from dashboard.office_hourly_status import (
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
from dashboard.office_scope_record import (
    SCOPE_PREFIX,
    _latest_scope_record,
    parse_scope_decisions,
)
from dashboard.office_status import (
    CANCELLED,
    DONE,
    STATUSES,
    StatusInfo,
)

__all__ = [
    "SCOPE_PREFIX",
    "HourlyCard",
    "HourlyReport",
    "_latest_scope_record",
    "build_report",
    "evidence_of",
    "load_recent_task_events",
    "load_report",
    "next_question_reminder",
    "parse_scope_decisions",
    "status_info_of",
    "status_of",
]

WINDOW = timedelta(hours=1)
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
    excluded_by_decision: list[tuple[RoomTaskRow, str, bool]] | None = None



def build_report(rows: list[RoomTaskRow], attentions: dict[tuple[str, str], Attention],
                 questions: list[RoomTaskQuestionRow], events: list[RoomTaskEventRow],
                 now: datetime, controls: list[RoomTaskEventRow] | None = None,
                 control_next: list[RoomTaskEventRow] | None = None,
                 transitions: list[RoomTaskEventRow] | None = None,
                 scope: ScopeConfig | None = None,
                 include_closed: bool = False,
                 pending_decisions: dict[tuple[str, str], str] | None = None,
                 scope_message_id: str | None = None,
                 excluded_backlog: dict[tuple[str, str], str] | None = None,
                 current_handoffs: dict[tuple[str, str], str] | None = None) -> HourlyReport:
    scope = scope or env_config()
    control_events = {(e.room, e.task_id): e for e in (controls or [])}
    control_by_task = {k: e.kind for k, e in control_events.items()}
    pending_decisions = pending_decisions or {}
    excluded_backlog = excluded_backlog or {}
    open_questions = {(q.room, q.task_id): q for q in questions if q.status == "open"}
    pending = [(r, pending_decisions[(r.room, r.task_id)]) for r in rows
               if (r.room, r.task_id) in pending_decisions
               and r.status not in ("done", "cancelled")]
    pending_keys = {(r.room, r.task_id) for r, _ in pending}
    excluded_keys = set(excluded_backlog)
    excluded_rows = [(r, excluded_backlog[(r.room, r.task_id)],
                      (r.room, r.task_id) in open_questions) for r in rows
                     if (r.room, r.task_id) in excluded_keys]
    split = apply_scope([r for r in rows if (r.room, r.task_id) not in pending_keys
                         and (r.room, r.task_id) not in excluded_keys],
                        now, scope, control_by_task,
                        frozenset(current_handoffs or {}))
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
                        "/".join(scope.active_only_names), pending, scope_message_id,
                        excluded_rows)


async def load_report(now: datetime | None = None,
                      include_closed: bool = False) -> HourlyReport:
    now = now or utc_naive_now()
    inputs = await load_hourly_inputs(now, WINDOW)
    rows, questions, controls = inputs.rows, inputs.questions, inputs.controls
    scope_record = _latest_scope_record(inputs.scope_rows)
    pending_decisions, excluded_backlog, current_handoffs = (
        scope_record[1] if scope_record else ({}, {}, {}))
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
    report = build_report(rows, attentions, questions, inputs.events, now, controls,
                          inputs.control_next, inputs.transitions,
                          include_closed=include_closed,
                          pending_decisions=pending_decisions,
                          scope_message_id=scope_message_id,
                          excluded_backlog=excluded_backlog,
                          current_handoffs=current_handoffs)
    runtime, hidden_runtime = scope_runtime(latest_runtime_snapshot(inputs.runtime_rows, now))
    return replace(report, runtime=runtime, hidden_runtime=hidden_runtime)

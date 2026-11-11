"""Сессия Claude Code: постановка в очередь, статус и выжимка → событие.

Одна сессия — один `source_event_id`, поэтому дописанная сессия (`--continue`
через день) обновляет свою выжимку, а не заводит вторую. Обновление идёт только
когда реплик стало больше: повторная присылка того же не гоняет модель и не
переembedd-ит событие. Сброс `triage_status` в pending обязателен — иначе
обновлённый текст останется с прежним embedding и поиск будет находить старое.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from vera_shared.db.models import ClaudeSessionQueueRow, EventRow
from vera_shared.text_chunks import clip_content
from vera_shared.timeutil import utc_naive_now

from gateway.claude_session_models import ClaudeSession, ClaudeSessionStatus
from gateway.claude_session_repo import (
    enqueue_session,
    get_queued_session,
    upsert_session_event,
)

log = logging.getLogger(__name__)

SOURCE = "claude_chat"


def body_text(d: dict[str, Any], project: str, branch: str | None) -> str:
    """Человекочитаемое тело события — его и увидит поиск по мозгу."""
    parts = [str(d.get("summary", "")).strip()]
    for label, key in (("Темы", "topics"), ("Решения", "decisions"),
                       ("Изменено", "changes"), ("Проблемы", "problems"),
                       ("Осталось", "open_ends"), ("Числа", "numbers")):
        vals = [str(v).strip() for v in (d.get(key) or []) if str(v).strip()]
        if vals:
            parts.append(f"{label}: " + "; ".join(vals))
    steps = [str(v).strip() for v in (d.get("outline") or []) if str(v).strip()]
    if steps:
        parts.append("Ход работы:\n" + "\n".join(f"— {s}" for s in steps))
    where = " / ".join(x for x in (project, branch) if x)
    if where:
        parts.append(f"Где: {where}")
    return "\n\n".join(p for p in parts if p)


def naive(value: datetime) -> datetime:
    """`events.occurred_at` — timestamp WITHOUT time zone: со зоной asyncpg падает."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def queue_session(body: ClaudeSession) -> tuple[bool, int]:
    """(поставлена ли в очередь, сколько реплик насчитано)."""
    turns = max(len(body.turns), body.turn_count or 0)
    incoming = {
        ClaudeSessionQueueRow.project_dir: body.project_dir,
        ClaudeSessionQueueRow.cwd: body.cwd,
        ClaudeSessionQueueRow.git_branch: body.git_branch,
        ClaudeSessionQueueRow.started_at: naive(body.started_at),
        ClaudeSessionQueueRow.ended_at: naive(body.ended_at),
        ClaudeSessionQueueRow.turns: [t.model_dump() for t in body.turns],
        ClaudeSessionQueueRow.turn_count: turns,
        ClaudeSessionQueueRow.status: "pending",
        ClaudeSessionQueueRow.attempts: 0,
        ClaudeSessionQueueRow.error: None,
        ClaudeSessionQueueRow.updated_at: utc_naive_now(),
    }
    queued = await enqueue_session(body.session_id, incoming, turns)
    if queued is None:
        log.info("claude: сессия %s уже осмыслена (%d реплик)", body.session_id, turns)
        return False, turns
    log.info("claude: сессия %s принята в очередь (%d реплик)", body.session_id, turns)
    return True, turns


async def session_status(session_id: str) -> ClaudeSessionStatus | None:
    row = await get_queued_session(session_id)
    if row is None:
        return None
    return ClaudeSessionStatus(status=row.status, turns=row.turn_count,
                               done_turns=row.done_turns, event_id=row.event_id,
                               attempts=row.attempts, error=row.error)


async def store_summary(row: ClaudeSessionQueueRow, distilled: dict[str, Any],
                        report: dict[str, Any]) -> int | None:
    """Выжимка → событие. Одна сессия остаётся одним событием.

    Ключи набора — сами атрибуты, а не их имена: строка "metadata" резолвится в
    `EventRow.metadata`, то есть в объект SQLAlchemy MetaData, и UPDATE падает с
    «has no attribute _bulk_update_tuples». Колонка в БД зовётся metadata, а
    атрибут — metadata_.
    """
    metadata = {
        "session_id": row.session_id,
        "project_dir": row.project_dir,
        "cwd": row.cwd,
        "git_branch": row.git_branch,
        "turns": row.turn_count,
        "started_at": row.started_at.isoformat(),
        "ended_at": row.ended_at.isoformat(),
        "topics": distilled.get("topics") or [],
        "author_role": "self",
        "author_label": "Я",
        **report,
    }
    fresh = {
        EventRow.content_text: clip_content(body_text(distilled, row.project_dir,
                                                      row.git_branch)),
        EventRow.metadata_: metadata,
        EventRow.occurred_at: row.started_at,
        EventRow.triage_status: "pending",
        EventRow.triage_error: None,
        EventRow.triage_started_at: None,
    }
    identity = {
        EventRow.source: SOURCE,
        EventRow.source_event_id: f"session:{row.session_id}",
        EventRow.account: "laptop",
        EventRow.category: "session",
    }
    return await upsert_session_event(identity, fresh, row.turn_count)

"""POST /v1/claude/session — рабочая сессия Claude Code одним событием.

Синк на ноутбуке читает `~/.claude/projects/**/*.jsonl` и присылает сессию
целиком. Раньше он лил каждую реплику отдельным событием — одна сессия давала
сотни событий с кодом внутри, и полезное в мозге тонуло в них.

Осмыслить сессию в самом запросе нельзя, и это измерено: одно окно на 21 тыс.
символов не уложилось в 120с ожидания брокера, а nginx обрывает `/v1/` по
дефолтным 60с. Поэтому здесь только приём: сессия ложится в
`claude_session_queue`, ответ 202, осмысляет `claude_session_worker`.

Одна сессия — один `source_event_id`, поэтому дописанная сессия (`--continue`
через день) обновляет свою выжимку, а не заводит вторую. Обновление идёт только
когда реплик стало больше: повторная присылка того же не гоняет модель и не
переembedd-ит событие. Сброс `triage_status` в pending обязателен — иначе
обновлённый текст останется с прежним embedding и поиск будет находить старое.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from vera_shared.db.engine import get_session
from vera_shared.db.models import ClaudeSessionQueueRow, EventRow
from vera_shared.timeutil import utc_naive_now

from gateway.auth import check_internal_secret

log = logging.getLogger(__name__)
router = APIRouter()

SOURCE = "claude_chat"
MAX_BODY = 8000


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class ClaudeSession(BaseModel):
    session_id: str = Field(max_length=64)
    project_dir: str = Field(max_length=255)
    started_at: datetime
    ended_at: datetime
    cwd: str | None = None
    git_branch: str | None = Field(default=None, max_length=255)
    turns: list[Turn] = Field(min_length=1)


class ClaudeSessionAccepted(BaseModel):
    ok: bool
    status: str
    turns: int
    # Реплик не прибавилось — сессия уже осмыслена в этом объёме.
    unchanged: bool = False


class ClaudeSessionStatus(BaseModel):
    status: str
    turns: int
    done_turns: int
    event_id: int | None = None
    attempts: int = 0
    error: str | None = None


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
    return value.astimezone(timezone.utc).replace(tzinfo=None)


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
        EventRow.content_text: body_text(distilled, row.project_dir,
                                         row.git_branch)[:MAX_BODY],
        EventRow.metadata_: metadata,
        EventRow.occurred_at: row.started_at,
        EventRow.triage_status: "pending",
        EventRow.triage_error: None,
        EventRow.triage_started_at: None,
    }
    async with get_session() as s:
        stmt = (
            pg_insert(EventRow)
            .values({
                EventRow.source: SOURCE,
                EventRow.source_event_id: f"session:{row.session_id}",
                EventRow.account: "laptop",
                EventRow.category: "session",
                **fresh,
            })
            .on_conflict_do_update(
                index_elements=["source", "source_event_id"],
                set_=fresh,
                # Реплик не прибавилось — не трогаем событие и его embedding.
                # Исключение — пустышка прошлого захода (distilled=false):
                # настоящая выжимка обязана её перезаписать.
                where=(func.coalesce(
                    EventRow.metadata_["turns"].as_integer(), 0) < row.turn_count)
                | (func.coalesce(
                    EventRow.metadata_["distilled"].as_boolean(), False).is_(False)),
            )
            .returning(EventRow.id)
        )
        return (await s.execute(stmt)).scalar_one_or_none()


@router.post("/v1/claude/session", response_model=ClaudeSessionAccepted,
             status_code=202)
async def accept_claude_session(
    body: ClaudeSession,
    response: Response,
    x_internal_secret: str | None = Header(default=None),
) -> ClaudeSessionAccepted:
    check_internal_secret(x_internal_secret)

    turns = len(body.turns)
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
    async with get_session() as s:
        stmt = (
            pg_insert(ClaudeSessionQueueRow)
            .values({ClaudeSessionQueueRow.session_id: body.session_id, **incoming})
            .on_conflict_do_update(
                index_elements=["session_id"],
                set_=incoming,
                # Осмысленное не переосмысляем: в очередь возвращаем только
                # сессию, которую дописали.
                where=ClaudeSessionQueueRow.done_turns < turns,
            )
            .returning(ClaudeSessionQueueRow.session_id)
        )
        queued = (await s.execute(stmt)).scalar_one_or_none()

    if queued is None:
        response.status_code = 200
        log.info("claude: сессия %s уже осмыслена (%d реплик)", body.session_id, turns)
        return ClaudeSessionAccepted(ok=True, status="done", turns=turns,
                                     unchanged=True)
    log.info("claude: сессия %s принята в очередь (%d реплик)", body.session_id, turns)
    return ClaudeSessionAccepted(ok=True, status="pending", turns=turns)


@router.get("/v1/claude/session/{session_id}", response_model=ClaudeSessionStatus)
async def claude_session_status(
    session_id: str,
    x_internal_secret: str | None = Header(default=None),
) -> ClaudeSessionStatus:
    check_internal_secret(x_internal_secret)
    async with get_session() as s:
        row = (await s.execute(
            select(ClaudeSessionQueueRow)
            .where(ClaudeSessionQueueRow.session_id == session_id)
        )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="сессия не присылалась")
    return ClaudeSessionStatus(status=row.status, turns=row.turn_count,
                               done_turns=row.done_turns, event_id=row.event_id,
                               attempts=row.attempts, error=row.error)

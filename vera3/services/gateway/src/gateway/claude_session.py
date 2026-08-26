"""POST /v1/claude/session — рабочая сессия Claude Code одним событием.

Синк на ноутбуке читает `~/.claude/projects/**/*.jsonl` и присылает сессию
целиком; сервер её осмысляет (`claude_distill`) и пишет ОДНУ выжимку. Раньше
синк лил каждую реплику отдельным событием — одна сессия давала сотни событий с
кодом внутри, и полезное в мозге тонуло в них.

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

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow

from gateway.auth import check_internal_secret
from gateway.claude_distill import distill

log = logging.getLogger(__name__)
router = APIRouter()

SOURCE = "claude_chat"
MAX_BODY = 8000


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class ClaudeSession(BaseModel):
    session_id: str
    project_dir: str
    started_at: datetime
    ended_at: datetime
    cwd: str | None = None
    git_branch: str | None = None
    turns: list[Turn] = Field(min_length=1)


class ClaudeSessionResult(BaseModel):
    ok: bool
    event_id: int | None = None
    # Ничего не поменялось — реплик не прибавилось, событие не тронуто.
    unchanged: bool = False
    summary: str | None = None


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


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@router.post("/v1/claude/session", response_model=ClaudeSessionResult)
async def ingest_claude_session(
    body: ClaudeSession,
    x_internal_secret: str | None = Header(default=None),
) -> ClaudeSessionResult:
    check_internal_secret(x_internal_secret)

    distilled, report = await distill(body.turns, project=body.project_dir,
                                      branch=body.git_branch)
    if not report["transcript_chars"]:
        return ClaudeSessionResult(ok=False)

    turns = len(body.turns)
    content = body_text(distilled, body.project_dir, body.git_branch)[:MAX_BODY]
    metadata = {
        "session_id": body.session_id,
        "project_dir": body.project_dir,
        "cwd": body.cwd,
        "git_branch": body.git_branch,
        "turns": turns,
        "started_at": body.started_at.isoformat(),
        "ended_at": body.ended_at.isoformat(),
        "topics": distilled.get("topics") or [],
        "author_role": "self",
        "author_label": "Я",
        **report,
    }
    # Ключи — сами атрибуты, а не их имена: строка "metadata" резолвится в
    # `EventRow.metadata`, то есть в объект SQLAlchemy MetaData, и UPDATE падает
    # с «MetaData object has no attribute _bulk_update_tuples». Колонка в БД
    # называется metadata, а атрибут — metadata_.
    fresh = {
        EventRow.content_text: content,
        EventRow.metadata_: metadata,
        EventRow.occurred_at: _naive(body.started_at),
        EventRow.triage_status: "pending",
        EventRow.triage_error: None,
        EventRow.triage_started_at: None,
    }

    async with get_session() as s:
        stmt = (
            pg_insert(EventRow)
            .values({
                EventRow.source: SOURCE,
                EventRow.source_event_id: f"session:{body.session_id}",
                EventRow.account: "laptop",
                EventRow.category: "session",
                **fresh,
            })
            .on_conflict_do_update(
                index_elements=["source", "source_event_id"],
                set_=fresh,
                # Реплик не прибавилось — не гоняем модель повторно и не
                # трогаем embedding: пересылка той же сессии бесплатна.
                where=func.coalesce(
                    EventRow.metadata_["turns"].as_integer(), 0) < turns,
            )
            .returning(EventRow.id)
        )
        event_id = (await s.execute(stmt)).scalar_one_or_none()

    if event_id is None:
        log.info("claude: сессия %s без новых реплик (%d)", body.session_id, turns)
        return ClaudeSessionResult(ok=True, unchanged=True)

    log.info("claude: сессия %s -> event=%s (%d реплик, %d симв., окон %d%s)",
             body.session_id, event_id, turns, report["transcript_chars"],
             report["windows"], ", ХВОСТ ОБРЕЗАН" if report["truncated"] else "")
    return ClaudeSessionResult(ok=True, event_id=event_id,
                               summary=distilled.get("summary"))

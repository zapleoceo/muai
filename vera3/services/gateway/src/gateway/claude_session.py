"""POST /v1/claude/session — рабочая сессия Claude Code одним событием.

Синк на ноутбуке читает `~/.claude/projects/**/*.jsonl` и присылает сессию
целиком. Раньше он лил каждую реплику отдельным событием — одна сессия давала
сотни событий с кодом внутри, и полезное в мозге тонуло в них.

Осмыслить сессию в самом запросе нельзя, и это измерено: одно окно на 21 тыс.
символов не уложилось в 120с ожидания брокера, а nginx обрывает `/v1/` по
дефолтным 60с. Поэтому здесь только приём: сессия ложится в
`claude_session_queue`, ответ 202, осмысляет `claude_session_worker`.
Логика — `claude_session_service`, SQL — `claude_session_repo`.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Response

from gateway.auth import check_internal_secret
from gateway.claude_session_models import (
    ClaudeSession,
    ClaudeSessionAccepted,
    ClaudeSessionStatus,
)

# store_summary — реэкспорт: claude_session_worker импортирует его отсюда.
from gateway.claude_session_service import (
    queue_session,
    session_status,
    store_summary,  # noqa: F401
)

router = APIRouter()


@router.post("/v1/claude/session", response_model=ClaudeSessionAccepted,
             status_code=202)
async def accept_claude_session(
    body: ClaudeSession,
    response: Response,
    x_internal_secret: str | None = Header(default=None),
) -> ClaudeSessionAccepted:
    check_internal_secret(x_internal_secret)
    queued, turns = await queue_session(body)
    if not queued:
        response.status_code = 200
        return ClaudeSessionAccepted(ok=True, status="done", turns=turns,
                                     unchanged=True)
    return ClaudeSessionAccepted(ok=True, status="pending", turns=turns)


@router.get("/v1/claude/session/{session_id}", response_model=ClaudeSessionStatus)
async def claude_session_status(
    session_id: str,
    x_internal_secret: str | None = Header(default=None),
) -> ClaudeSessionStatus:
    check_internal_secret(x_internal_secret)
    status = await session_status(session_id)
    if status is None:
        raise HTTPException(status_code=404, detail="сессия не присылалась")
    return status

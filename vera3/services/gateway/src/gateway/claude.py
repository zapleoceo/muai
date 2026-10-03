"""POST /v1/claude/remember — facts coming from Claude conversations.

The MCP server (`services/mcp`, tool `remember`, and the legacy stdio
`vera-mcp`) calls this logic whenever Claude decides a turn contained a
fact / decision / preference worth keeping. The write path and its
two-layer dedup (exact sha256 + semantic cosine ≥0.92 within 7 days) live
in `vera_shared.memory.remember` so both transports share one implementation.

Returns {ok, event_id, deduped, dedup_reason}. The caller surfaces this so
the agent knows whether to mention 'already known' in chat.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field
from vera_shared.memory.remember import remember_fact

from gateway.auth import check_internal_secret

router = APIRouter()


class RememberRequest(BaseModel):
    text: str = Field(min_length=3, max_length=8000)
    kind: Literal["fact", "decision", "todo", "preference"] = "fact"
    context: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=10)


class RememberResponse(BaseModel):
    ok: bool
    event_id: int | None
    deduped: bool
    dedup_reason: Literal["exact", "semantic", None] = None
    similar_event_id: int | None = None
    similarity: float | None = None


@router.post("/v1/claude/remember", response_model=RememberResponse)
async def remember(
    body: RememberRequest,
    x_internal_secret: str | None = Header(default=None),
) -> RememberResponse:
    check_internal_secret(x_internal_secret)
    outcome = await remember_fact(body.text, body.kind, body.context, body.tags)
    return RememberResponse(
        ok=True, event_id=outcome.event_id, deduped=outcome.deduped,
        dedup_reason=outcome.dedup_reason,
        similar_event_id=outcome.similar_event_id, similarity=outcome.similarity,
    )

"""POST /v1/claude/remember — facts coming from Claude conversations.

The MCP server `vera-mcp` calls this endpoint whenever Claude decides a
turn contained a fact / decision / preference worth keeping. Vera writes
it to `events` with `source='claude'`; triage picks it up like any other
event and embeds + entity-extracts.

Dedup — two layers, both run server-side so the MCP client stays dumb:

1. Exact (sha256 of text). Same text → ON CONFLICT DO NOTHING.
2. Semantic. Embed the text via the broker, search for nearest
   neighbour among claude-source events from the last 7 days. If
   cosine ≥ 0.92 → return deduped (don't write).

Returns {ok, event_id, deduped, dedup_reason}. The MCP layer surfaces
this to Claude so it knows whether to mention 'already known' in chat.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.llm.client import LLMCallFailed, embed

from gateway.config import get_settings

log = logging.getLogger(__name__)
router = APIRouter()


SEMANTIC_DEDUP_THRESHOLD = 0.92
SEMANTIC_LOOKBACK_DAYS = 7


def _check_internal_secret(provided: str | None) -> None:
    expected = get_settings().internal_secret
    if expected and provided != expected:
        raise HTTPException(401, "invalid internal secret")


def _content_hash(text: str) -> str:
    """Stable 16-char hash → source_event_id. Same text always dedupes."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


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


async def _find_semantic_neighbour(
    text: str,
) -> tuple[int, float] | None:
    """Embed text, scan claude events for last 7d, return (id, sim) of best
    match if similarity ≥ threshold. Returns None on broker failure or no hit.
    """
    try:
        vectors = await embed(text)
    except LLMCallFailed as e:
        log.warning("semantic dedup skipped — embed failed: %s", e)
        return None
    if not vectors:
        return None
    q_vec = vectors[0]

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=SEMANTIC_LOOKBACK_DAYS
    )
    async with get_session() as s:
        rows = (
            await s.execute(
                select(EventRow.id, EventRow.embedding_voyage_3)
                .where(
                    EventRow.source == "claude",
                    EventRow.received_at >= since,
                    EventRow.embedding_voyage_3.is_not(None),
                )
                .order_by(EventRow.received_at.desc())
                .limit(500)
            )
        ).all()

    best_id: int | None = None
    best_sim = 0.0
    for row in rows:
        sim = _cosine(q_vec, row.embedding_voyage_3)
        if sim > best_sim:
            best_sim, best_id = sim, row.id
    if best_id is not None and best_sim >= SEMANTIC_DEDUP_THRESHOLD:
        return best_id, best_sim
    return None


@router.post("/v1/claude/remember", response_model=RememberResponse)
async def remember(
    body: RememberRequest,
    x_internal_secret: str | None = Header(default=None),
) -> RememberResponse:
    _check_internal_secret(x_internal_secret)

    text = body.text.strip()
    src_id = _content_hash(text)

    # Layer 1 — exact dedup via UNIQUE (source, source_event_id).
    metadata: dict[str, Any] = {"kind": body.kind}
    if body.context:
        metadata["context"] = body.context
    if body.tags:
        metadata["tags"] = body.tags

    async with get_session() as s:
        stmt = (
            pg_insert(EventRow)
            .values(
                source="claude",
                source_event_id=src_id,
                category=body.kind,
                content_text=text,
                metadata_=metadata,
                occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
                triage_status="pending",
            )
            .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
            .returning(EventRow.id)
        )
        result = await s.execute(stmt)
        event_id = result.scalar_one_or_none()

        if event_id is None:
            existing = await s.execute(
                select(EventRow.id).where(
                    EventRow.source == "claude",
                    EventRow.source_event_id == src_id,
                )
            )
            existing_id = existing.scalar_one_or_none()
            log.info("remember: exact dedup hit, event=%s", existing_id)
            return RememberResponse(
                ok=True, event_id=existing_id,
                deduped=True, dedup_reason="exact",
            )

    # Layer 2 — semantic dedup. We've already inserted; if a near-duplicate
    # exists, mark our brand-new row as 'superseded' so triage skips it.
    # Trade-off: one extra row per dup vs. doing the embed BEFORE insert
    # (which doubles latency on the common case of no dup).
    neighbour = await _find_semantic_neighbour(text)
    if neighbour is not None:
        sim_id, sim = neighbour
        async with get_session() as s:
            await s.execute(
                EventRow.__table__.update()
                .where(EventRow.id == event_id)
                .values(triage_status="superseded",
                         triage_metadata={"superseded_by": sim_id,
                                          "similarity": sim})
            )
        log.info("remember: semantic dedup, event=%s superseded by %s (sim=%.3f)",
                 event_id, sim_id, sim)
        return RememberResponse(
            ok=True, event_id=event_id,
            deduped=True, dedup_reason="semantic",
            similar_event_id=sim_id, similarity=sim,
        )

    log.info("remember: new event=%s kind=%s", event_id, body.kind)
    return RememberResponse(ok=True, event_id=event_id, deduped=False)

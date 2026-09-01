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
import json
import logging
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.vectors import as_pg_vector, vector_column_available
from vera_shared.llm.client import LLMCallFailed, embed
from vera_shared.timeutil import utc_naive_now

from gateway.auth import check_internal_secret

log = logging.getLogger(__name__)
router = APIRouter()


SEMANTIC_DEDUP_THRESHOLD = 0.92
SEMANTIC_LOOKBACK_DAYS = 7


def _content_hash(text: str) -> str:
    """Stable 16-char hash → source_event_id. Same text always dedupes."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # strict=True безопасен: разная длина отсеяна строкой выше
    dot = sum(x * y for x, y in zip(a, b, strict=True))
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
) -> tuple[list[float] | None, tuple[int, float] | None]:
    """Embed text, scan claude events for last 7d. Returns (q_vec, match):
    match = (id, sim) при similarity ≥ threshold, иначе None; q_vec отдаём
    вызывающему — он пишет его в event_embeddings сразу (иначе «слепое
    окно»: пока триаж не эмбеднул событие, его не видит следующий дедуп).
    (None, None) — broker failure."""
    try:
        vectors = await embed(text)
    except LLMCallFailed as e:
        log.warning("semantic dedup skipped — embed failed: %s", e)
        return None, None
    if not vectors:
        return None, None
    q_vec = vectors[0]

    since = utc_naive_now() - timedelta(
        days=SEMANTIC_LOOKBACK_DAYS
    )
    # Эмбеддинги вынесены в event_embeddings (миграция 011) — джойним.
    if await vector_column_available():
        # Ближайшего ищет Postgres по индексу. Оператор <=> — косинусное
        # РАССТОЯНИЕ, поэтому сходство = 1 - расстояние.
        async with get_session() as s:
            row = (await s.execute(sa_text("""
                SELECT e.id, 1 - (ee.embedding_vec <=> CAST(:q AS vector)) AS sim
                FROM events e
                JOIN event_embeddings ee ON ee.event_id = e.id
                WHERE e.source = 'claude' AND e.received_at >= :since
                  AND ee.embedding_vec IS NOT NULL
                ORDER BY ee.embedding_vec <=> CAST(:q AS vector)
                LIMIT 1
            """), {"since": since, "q": as_pg_vector(q_vec)})).first()
        if row is not None and row[1] >= SEMANTIC_DEDUP_THRESHOLD:
            return q_vec, (row[0], float(row[1]))
        return q_vec, None

    # Пока бэкфил не прошёл: 500 векторов по 1024 float разбираются из
    # JSON-текста и перебираются в Python. Это и есть та цена, ради которой
    # делалась миграция 030.
    async with get_session() as s:
        rows = (
            await s.execute(sa_text("""
                SELECT e.id, ee.embedding
                FROM events e
                JOIN event_embeddings ee ON ee.event_id = e.id
                WHERE e.source = 'claude' AND e.received_at >= :since
                ORDER BY e.received_at DESC
                LIMIT 500
            """), {"since": since})
        ).all()

    best_id: int | None = None
    best_sim = 0.0
    for row in rows:
        sim = _cosine(q_vec, row[1])
        if sim > best_sim:
            best_sim, best_id = sim, row[0]
    if best_id is not None and best_sim >= SEMANTIC_DEDUP_THRESHOLD:
        return q_vec, (best_id, best_sim)
    return q_vec, None


@router.post("/v1/claude/remember", response_model=RememberResponse)
async def remember(
    body: RememberRequest,
    x_internal_secret: str | None = Header(default=None),
) -> RememberResponse:
    check_internal_secret(x_internal_secret)

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
                occurred_at=utc_naive_now(),
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
    q_vec, neighbour = await _find_semantic_neighbour(text)
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

    # Эмбеддинг уже посчитан для дедупа — пишем сразу, закрывая слепое окно
    # (следующий remember() с похожим текстом увидит это событие, не дожидаясь
    # триажа). Триаж потом перезапишет тем же вектором — безвредно.
    if q_vec is not None:
        async with get_session() as s:
            await s.execute(sa_text("""
                INSERT INTO event_embeddings (event_id, embedding)
                VALUES (:eid, CAST(:emb AS jsonb))
                ON CONFLICT (event_id) DO UPDATE SET embedding = EXCLUDED.embedding
            """), {"eid": event_id, "emb": json.dumps(q_vec)})

    log.info("remember: new event=%s kind=%s", event_id, body.kind)
    return RememberResponse(ok=True, event_id=event_id, deduped=False)

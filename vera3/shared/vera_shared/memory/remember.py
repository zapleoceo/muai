"""Запись факта в `events` с `source='claude'` и двухслойным дедупом.

Общая логика для `POST /v1/claude/remember` (шлюз) и MCP-инструмента
`remember`. Дедуп целиком серверный, клиенту ничего проверять не нужно:

1. Точный — sha256 текста → UNIQUE (source, source_event_id).
2. Смысловой — эмбеддинг через брокер, ближайший сосед среди claude-событий
   за 7 дней; косинус ≥ 0.92 → событие помечается `superseded`.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.vectors import (
    VEC_TYPE,
    as_pg_vector,
    embedding_upsert,
    vector_column_available,
)
from vera_shared.events.visibility import HIDDEN_STATUS
from vera_shared.llm.client import LLMCallFailed, embed
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

SEMANTIC_DEDUP_THRESHOLD = 0.92
SEMANTIC_LOOKBACK_DAYS = 7

Kind = Literal["fact", "decision", "todo", "preference"]


@dataclass
class RememberOutcome:
    event_id: int | None
    deduped: bool
    dedup_reason: Literal["exact", "semantic", None] = None
    similar_event_id: int | None = None
    similarity: float | None = None


def _content_hash(text: str) -> str:
    """Stable 16-char hash → source_event_id. Same text always dedupes."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


async def _find_semantic_neighbour(
    text: str,
) -> tuple[list[float] | None, tuple[int, float] | None]:
    """Embed text, scan claude events for last 7d. Returns (q_vec, match):
    match = (id, sim) при similarity ≥ threshold, иначе None; q_vec отдаём
    вызывающему — он пишет его в event_embeddings сразу (иначе «слепое
    окно»: пока триаж не эмбеддил событие, его не видит следующий дедуп).
    (None, None) — broker failure."""
    try:
        vectors = await embed(text)
    except LLMCallFailed as e:
        log.warning("semantic dedup skipped — embed failed: %s", e)
        return None, None
    if not vectors:
        return None, None
    q_vec = vectors[0]

    since = utc_naive_now() - timedelta(days=SEMANTIC_LOOKBACK_DAYS)
    has_vec = await vector_column_available()
    best: tuple[int, float] | None = None
    visible = f"e.triage_status <> '{HIDDEN_STATUS}'"
    async with get_session() as s:
        if has_vec:
            # Оператор <=> — косинусное РАССТОЯНИЕ, сходство = 1 - расстояние.
            # Индекс не нужен: claude-событий за 7 дней десятки.
            row = (await s.execute(sa_text(f"""
                SELECT e.id, 1 - (ee.embedding_vec <=> CAST(:q AS {VEC_TYPE})) AS sim
                FROM events e
                JOIN event_embeddings ee ON ee.event_id = e.id
                WHERE e.source = 'claude' AND e.received_at >= :since
                  AND {visible} AND ee.embedding_vec IS NOT NULL
                ORDER BY ee.embedding_vec <=> CAST(:q AS {VEC_TYPE})
                LIMIT 1
            """), {"since": since, "q": as_pg_vector(q_vec)})).first()
            if row is not None:
                best = (row[0], float(row[1]))
        # Без колонки — все строки; с колонкой — только те, до которых бэкфил
        # ещё не дошёл, иначе частично залитая колонка молча сужала бы дедуп.
        unfilled = " AND ee.embedding_vec IS NULL" if has_vec else ""
        rows = (await s.execute(sa_text(f"""
            SELECT e.id, ee.embedding
            FROM events e
            JOIN event_embeddings ee ON ee.event_id = e.id
            WHERE e.source = 'claude' AND e.received_at >= :since
              AND {visible}{unfilled}
            ORDER BY e.received_at DESC
            LIMIT 500
        """), {"since": since})).all()

    for row in rows:
        sim = _cosine(q_vec, row[1])
        if sim > (best[1] if best else 0.0):
            best = (row[0], sim)
    if best is not None and best[1] >= SEMANTIC_DEDUP_THRESHOLD:
        return q_vec, best
    return q_vec, None


async def _insert_or_find(src_id: str, text: str, kind: str,
                          metadata: dict[str, Any]) -> tuple[int | None, bool]:
    """(event_id, created). created=False — точный дубль, id существующего."""
    async with get_session() as s:
        stmt = (
            pg_insert(EventRow)
            .values(
                source="claude", source_event_id=src_id, category=kind,
                content_text=text, metadata_=metadata,
                occurred_at=utc_naive_now(), triage_status="pending",
            )
            .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
            .returning(EventRow.id)
        )
        event_id = (await s.execute(stmt)).scalar_one_or_none()
        if event_id is not None:
            return event_id, True
        existing = (await s.execute(
            select(EventRow.id).where(EventRow.source == "claude",
                                      EventRow.source_event_id == src_id)
        )).scalar_one_or_none()
        return existing, False


async def remember_fact(text: str, kind: Kind = "fact",
                        context: str | None = None,
                        tags: list[str] | None = None) -> RememberOutcome:
    text = text.strip()
    metadata: dict[str, Any] = {"kind": kind}
    if context:
        metadata["context"] = context
    if tags:
        metadata["tags"] = tags

    event_id, created = await _insert_or_find(_content_hash(text), text, kind, metadata)
    if not created:
        log.info("remember: exact dedup hit, event=%s", event_id)
        return RememberOutcome(event_id, True, "exact")

    # Вставка уже сделана: при почти-дубле новая строка помечается
    # `superseded`, чтобы триаж её пропустил. Эмбеддинг ДО вставки удвоил бы
    # задержку в частом случае «дубля нет».
    q_vec, neighbour = await _find_semantic_neighbour(text)
    if neighbour is not None:
        sim_id, sim = neighbour
        async with get_session() as s:
            await s.execute(
                EventRow.__table__.update()
                .where(EventRow.id == event_id)
                .values(triage_status="superseded",
                        triage_metadata={"superseded_by": sim_id, "similarity": sim})
            )
        log.info("remember: semantic dedup, event=%s superseded by %s (sim=%.3f)",
                 event_id, sim_id, sim)
        return RememberOutcome(event_id, True, "semantic", sim_id, sim)

    # Вектор уже посчитан для дедупа — пишем сразу, закрывая слепое окно.
    if q_vec is not None:
        stmt, params = embedding_upsert(event_id, q_vec, await vector_column_available())
        async with get_session() as s:
            await s.execute(stmt, params)

    log.info("remember: new event=%s kind=%s", event_id, kind)
    return RememberOutcome(event_id, False)

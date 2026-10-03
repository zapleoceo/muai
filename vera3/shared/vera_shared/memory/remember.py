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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.vectors import (
    VEC_TYPE,
    as_pg_vector,
    embedding_upsert,
)
from vera_shared.events import edit as event_edit
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
    unhidden: bool = False


def _content_hash(text: str) -> str:
    """Stable 16-char hash → source_event_id. Same text always dedupes."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


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
    visible = f"e.triage_status <> '{HIDDEN_STATUS}'"
    async with get_session() as s:
        # Оператор <=> — косинусное РАССТОЯНИЕ, сходство = 1 - расстояние.
        # Индекс не нужен: claude-событий за 7 дней десятки.
        row = (await s.execute(sa_text(f"""
            SELECT e.id, 1 - (ee.embedding_vec <=> CAST(:q AS {VEC_TYPE})) AS sim
            FROM events e
            JOIN event_embeddings ee ON ee.event_id = e.id
            WHERE e.source = 'claude' AND e.received_at >= :since
              AND {visible}
            ORDER BY ee.embedding_vec <=> CAST(:q AS {VEC_TYPE})
            LIMIT 1
        """), {"since": since, "q": as_pg_vector(q_vec)})).first()
    best = None if row is None else (row[0], float(row[1]))
    if best is not None and best[1] >= SEMANTIC_DEDUP_THRESHOLD:
        return q_vec, best
    return q_vec, None


WrittenHook = Callable[[AsyncSession, RememberOutcome], Awaitable[None]]


async def _existing(src_id: str) -> tuple[int, str] | None:
    """(id, triage_status) уже записанного факта с таким текстом."""
    async with get_session() as s:
        row = (await s.execute(
            select(EventRow.id, EventRow.triage_status).where(
                EventRow.source == "claude", EventRow.source_event_id == src_id)
        )).first()
    return None if row is None else (row[0], row[1])


async def _revive_hidden(event_id: int,
                         on_written: WrittenHook | None) -> RememberOutcome:
    """Повторный remember текста, который скрыли (в т.ч. откатом remember):
    без этого он молча ничего не делал бы — точный дубль скрытого события.
    Событие возвращается из скрытия, и это тоже попадает в журнал."""
    async with get_session() as s:
        await event_edit.set_hidden(s, event_id, hidden=False)
        outcome = RememberOutcome(event_id, True, "exact", unhidden=True)
        if on_written is not None:
            await on_written(s, outcome)
    log.info("remember: hidden event=%s unhidden by repeated remember", event_id)
    return outcome


async def remember_fact(text: str, kind: Kind = "fact",
                        context: str | None = None,
                        tags: list[str] | None = None, *,
                        on_written: WrittenHook | None = None) -> RememberOutcome:
    """`on_written` вызывается в ТОЙ ЖЕ транзакции, что вставка события, и только
    когда строка создана или возвращена из скрытия (не при обычном точном дубле): запись журнала MCP
    либо появляется вместе с событием, либо не появляется вовсе."""
    text = text.strip()
    src_id = _content_hash(text)
    existing = await _existing(src_id)
    if existing is not None:
        if existing[1] == HIDDEN_STATUS:
            return await _revive_hidden(existing[0], on_written)
        log.info("remember: exact dedup hit, event=%s", existing[0])
        return RememberOutcome(existing[0], True, "exact")

    metadata: dict[str, Any] = {"kind": kind}
    if context:
        metadata["context"] = context
    if tags:
        metadata["tags"] = tags

    # Смысловой дубль ищем ДО вставки: тогда событие пишется сразу в итоговом
    # статусе, и вставка, вектор и журнал уходят одной транзакцией.
    q_vec, neighbour = await _find_semantic_neighbour(text)
    values: dict[str, Any] = {
        "source": "claude", "source_event_id": src_id, "category": kind,
        "content_text": text, "metadata_": metadata,
        "occurred_at": utc_naive_now(), "triage_status": "pending",
    }
    if neighbour is not None:
        values["triage_status"] = "superseded"
        values["triage_metadata"] = {"superseded_by": neighbour[0],
                                     "similarity": neighbour[1]}
    async with get_session() as s:
        event_id = (await s.execute(
            pg_insert(EventRow).values(**values)
            .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
            .returning(EventRow.id)
        )).scalar_one_or_none()
        if event_id is None:
            raced = await _existing(src_id)
            return RememberOutcome(raced[0] if raced else None, True, "exact")
        if neighbour is not None:
            outcome = RememberOutcome(event_id, True, "semantic", *neighbour)
        else:
            outcome = RememberOutcome(event_id, False)
            if q_vec is not None:
                await _write_vector(s, event_id, q_vec)
        if on_written is not None:
            await on_written(s, outcome)
    log.info("remember: event=%s kind=%s dedup=%s", event_id, kind, outcome.dedup_reason)
    return outcome


async def _write_vector(s: AsyncSession, event_id: int, q_vec: list[float]) -> None:
    """Вектор, уже посчитанный для дедупа, — сразу в event_embeddings."""
    # Отдельная точка сохранения: отказ записи вектора не должен терять сам
    # факт. Событие останется без вектора, его подберёт цикл доэмбеддинга
    # brain-triage (reembed). Так факт переживает и окно накатки миграций
    # эмбеддингов, где схема колонки на мгновение расходится с кодом.
    stmt, params = embedding_upsert(event_id, q_vec)
    try:
        async with s.begin_nested():
            await s.execute(stmt, params)
    except DBAPIError as e:
        log.warning("remember: вектор события %s не записан (%s) — доэмбеддит reembed",
                    event_id, type(e).__name__)

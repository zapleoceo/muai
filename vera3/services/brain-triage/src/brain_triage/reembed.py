"""Доэмбеддинг: события со статусом done, у которых вектор не записался.

Триаж помечает событие done и при отказе эмбеддинга (брокер лёг, пришло
меньше векторов, чем просили) — вектор тогда пропадает навсегда. Цикл раз в
REEMBED_INTERVAL_S берёт порцию таких событий, самые новые первыми.

Курсор по id идёт сверху вниз: события-заглушки (см. text_quality) вектора не
получают и подходили бы под запрос каждый раз, выедая порцию. Проход
заканчивается, когда выборка короче лимита, и курсор сбрасывается.
Запрос — обратный скан events_pkey + anti-join по PK event_embeddings.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from vera_shared.db.engine import get_session
from vera_shared.llm.circuit import llm_cooldown_remaining_s
from vera_shared.text_chunks import llm_excerpt
from vera_shared.text_quality import is_contentless

from brain_triage.chunks import embed_event_chunks
from brain_triage.config import REEMBED_BATCH, REEMBED_INTERVAL_S
from brain_triage.embeddings import write_embeddings
from brain_triage.postprocess import SKIP_EMBED_SOURCES
from brain_triage.triage_calls import _embed_batch

log = logging.getLogger(__name__)

_SCAN_FACTOR = 4
_TOP = 9223372036854775807

_CANDIDATES_SQL = text("""
    SELECT e.id, e.content_text
    FROM events e
    WHERE e.triage_status = 'done'
      AND e.content_text <> ''
      AND e.id < :before
      AND e.source <> ALL(:skip)
      AND NOT EXISTS (SELECT 1 FROM event_embeddings m WHERE m.event_id = e.id)
    ORDER BY e.id DESC
    LIMIT :lim
""")


async def _fetch_candidates(before_id: int, limit: int) -> list[tuple[int, str]]:
    async with get_session() as s:
        rows = await s.execute(_CANDIDATES_SQL, {
            "before": before_id, "skip": sorted(SKIP_EMBED_SOURCES), "lim": limit,
        })
        return [(r.id, r.content_text) for r in rows]


async def reembed_once(before_id: int | None = None) -> tuple[int | None, int]:
    """Один проход. Возвращает (курсор следующего прохода | None, сколько записано)."""
    if await llm_cooldown_remaining_s("embed") > 0:
        return before_id, 0
    scan = REEMBED_BATCH * _SCAN_FACTOR
    candidates = await _fetch_candidates(before_id or _TOP, scan)
    picked: list[tuple[int, str]] = []
    cursor = before_id
    for eid, body in candidates:
        cursor = eid
        if not is_contentless(body):
            picked.append((eid, body))
            if len(picked) >= REEMBED_BATCH:
                break
    exhausted = len(candidates) < scan and len(picked) < REEMBED_BATCH
    next_cursor = None if exhausted else cursor
    if not picked:
        return next_cursor, 0
    vectors = await _embed_batch([llm_excerpt(body) for _, body in picked])
    pairs = [(eid, vec) for (eid, _), vec in zip(picked, vectors, strict=True)
             if vec is not None]
    if not pairs:
        return before_id, 0   # брокер не ответил — повторим с того же места
    written = await write_embeddings(pairs)
    bodies = dict(picked)
    await embed_event_chunks([(eid, bodies[eid]) for eid, _ in pairs])
    return next_cursor, written


async def reembed_loop() -> None:
    cursor: int | None = None
    while True:
        await asyncio.sleep(REEMBED_INTERVAL_S)
        try:
            cursor, written = await reembed_once(cursor)
            if written:
                log.info("reembed: записано %d векторов, курсор=%s", written, cursor)
        except Exception as e:
            log.warning("reembed error: %s", e)

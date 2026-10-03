"""Доэмбеддинг: события со статусом done, у которых вектор не записался.

Триаж помечает событие done и при отказе эмбеддинга (брокер лёг, пришло
меньше векторов, чем просили) — вектор тогда пропадает навсегда. Цикл раз в
REEMBED_INTERVAL_S берёт порцию таких событий.

Каждый проход — два ОГРАНИЧЕННЫХ по диапазону id окна, а не один спуск по всей
таблице (ORDER BY id DESC LIMIT без границы при редких кандидатах обходит все
470k строк):
  * голова — последние HEAD_SPAN id: свежая дыра закрывается за один цикл, а
    не после многодневного прохода; окно читается целиком, чтобы заглушки
    (см. text_quality), которые никогда не получат вектор, не вытесняли
    настоящие события из выборки;
  * хвост — окно в TAIL_SPAN id ниже курсора; курсор сдвигается на окно
    независимо от того, нашлось ли в нём что-то, и по достижении дна
    сбрасывается — старые дыры подбираются медленно, но проход конечен.
Оба запроса — диапазонный скан events_pkey + anti-join по PK event_embeddings.
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

HEAD_SPAN = 2000
TAIL_SPAN = 20000
_SCAN_FACTOR = 4

Candidate = tuple[int, str, str]   # (id, source, content_text)

_CANDIDATES_SQL = text("""
    SELECT e.id, e.source, e.content_text
    FROM events e
    WHERE e.id >= :lo AND e.id < :hi
      AND e.triage_status = 'done'
      AND e.content_text <> ''
      AND e.source <> ALL(:skip)
      AND NOT EXISTS (SELECT 1 FROM event_embeddings m WHERE m.event_id = e.id)
    ORDER BY e.id DESC
    LIMIT :lim
""")


async def _max_event_id() -> int:
    async with get_session() as s:
        return int((await s.execute(text("SELECT COALESCE(MAX(id), 0) FROM events"))).scalar_one())


async def _fetch_candidates(lo: int, hi: int, limit: int) -> list[Candidate]:
    async with get_session() as s:
        rows = await s.execute(_CANDIDATES_SQL, {
            "lo": lo, "hi": hi, "skip": sorted(SKIP_EMBED_SOURCES), "lim": limit,
        })
        return [(r.id, r.source, r.content_text) for r in rows]


def _pick(candidates: list[Candidate], limit: int) -> tuple[list[Candidate], int | None]:
    """(выбранные, id последнего просмотренного | None если ничего не просмотрено)."""
    picked: list[Candidate] = []
    last: int | None = None
    for cand in candidates:
        last = cand[0]
        if not is_contentless(cand[2], cand[1]):
            picked.append(cand)
            if len(picked) >= limit:
                break
    return picked, last


async def _tail_window(cursor: int | None, top: int) -> tuple[list[Candidate], int | None]:
    """Выбранные из окна ниже курсора и новый курсор (None — дно, начать заново)."""
    hi = cursor if cursor is not None else max(top - HEAD_SPAN, 0) + 1
    lo = max(hi - TAIL_SPAN, 0)
    scan = REEMBED_BATCH * _SCAN_FACTOR
    candidates = await _fetch_candidates(lo, hi, scan)
    picked, last = _pick(candidates, REEMBED_BATCH)
    if len(picked) >= REEMBED_BATCH and last is not None:
        return picked, last        # порция полна — остаток окна на следующий цикл
    if len(candidates) >= scan and last is not None:
        return picked, last        # выборка упёрлась в лимит из одних заглушек
    return picked, (lo if lo > 0 else None)


async def reembed_once(cursor: int | None = None) -> tuple[int | None, int]:
    """Один проход. Возвращает (курсор хвоста, сколько векторов записано)."""
    if await llm_cooldown_remaining_s("embed") > 0:
        return cursor, 0
    top = await _max_event_id()
    head = await _fetch_candidates(max(top - HEAD_SPAN, 0) + 1, top + 1, HEAD_SPAN)
    head_picked, _ = _pick(head, REEMBED_BATCH)
    tail_picked, next_cursor = await _tail_window(cursor, top)
    picked = head_picked + tail_picked
    if not picked:
        return next_cursor, 0
    vectors = await _embed_batch([llm_excerpt(body) for _, _, body in picked])
    pairs = [(eid, vec) for (eid, _, _), vec in zip(picked, vectors, strict=True)
             if vec is not None]
    if not pairs:
        return cursor, 0   # брокер не ответил — повторим с того же места
    written = await write_embeddings(pairs)
    bodies = {eid: body for eid, _, body in picked}
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

"""Worker loop: SELECT pending events → triage + embed → UPDATE.

Concurrency model:
- N реплик через docker compose `--scale brain-triage=N`
- Каждая реплика берёт batch через `UPDATE ... WHERE id IN (SELECT FOR UPDATE
  SKIP LOCKED) RETURNING *` — реплики не дерутся за одни и те же события
- triage_started_at используется watchdog'ом чтобы вернуть зависшие
  (а НЕ received_at — иначе старые pending мгновенно реверится при подборе)

Responsibilities live in sibling modules (see docs/architecture.md):
prompts.py (templates), schemas.py (json_schema defs), postprocess.py
(nature/project validation), claim.py (claim-batch query + group
classification/chunking), triage_calls.py (LLM calls), concurrency.py
(semaphore wrappers), project_override.py (deterministic project fixup),
background_loops.py (watchdog + retry). This file only owns
process_pending()'s orchestration — claim → embed → dispatch → write —
and main_loop(); its transaction boundaries (triage-status write /
embedding upsert / project override each in their OWN session) are
deliberate, see comments below.
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import text, update
from vera_shared.control import backfill_minute_allowance, is_backfill_paused
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow

from brain_triage.background_loops import (
    _bg_tasks,
    _retry_failed_loop,
    _safe_rel_extract,
    _watchdog_loop,
)
from brain_triage.claim import _chunk_group_rows, _claim_batch, chat_kind
from brain_triage.concurrency import (
    _process_group_chunk_with_sem,
    _process_one_with_sem,
)
from brain_triage.config import (
    BATCH_SIZE,
    CONCURRENCY,
    PACE_BETWEEN_S,
    POLL_INTERVAL_S,
    WORKER_ID,
)
from brain_triage.postprocess import NATURE_BY_SOURCE, SKIP_EMBED_SOURCES
from brain_triage.project_override import apply_project_override
from brain_triage.triage_calls import _embed_batch

log = logging.getLogger(__name__)


async def process_pending() -> int:
    """Захватить batch, эмбедить parallel, триаж concurrent, UPDATE."""
    if await is_backfill_paused():
        return 0   # paused from dashboard — skip claiming, main loop sleeps
    # Even-tempo rate limit: claim at most this minute's remaining budget.
    allowance = await backfill_minute_allowance()
    if allowance is not None and allowance <= 0:
        return 0   # rate reached — main loop sleeps, recheck next cycle
    batch = BATCH_SIZE if allowance is None else min(BATCH_SIZE, allowance)
    rows = await _claim_batch(batch)
    if not rows:
        return 0

    log.info("[%s] claimed batch of %d events", WORKER_ID, len(rows))

    # Источники-намерения (vera_chat, perplexity) не эмбеддим — их вектора
    # засоряют семантический поиск. Эмбеддим только события мира.
    embed_idx = [i for i, r in enumerate(rows) if r.source not in SKIP_EMBED_SOURCES]
    embed_texts = [(rows[i].content_text or "")[:8000] for i in embed_idx]
    embed_vectors = await _embed_batch(embed_texts)
    # by event_id, НЕ by position — группировка ниже переупорядочивает rows
    # (single_rows + group-chunks), positional zip() с embeddings был бы багом:
    # embedding события A мог бы приклеиться к triage-результату события B.
    embeddings_by_id: dict[int, list[float] | None] = {r.id: None for r in rows}
    for pos, vec in zip(embed_idx, embed_vectors):
        embeddings_by_id[rows[pos].id] = vec

    # Групповые telegram-сообщения (супергруппы + легаси Chat) батчатся по
    # TRIAGE_GROUP_BATCH_SIZE в один LLM-вызов — короткие тексты, экономия
    # call-budget под rate limiter. Каналы/личка/остальные источники — как
    # раньше, по одному, каждое сообщение разбирается отдельно.
    group_ids = {r.id for r in rows
                 if r.source == "telegram" and chat_kind(r) == "group"}
    group_rows = [r for r in rows if r.id in group_ids]
    single_rows = [r for r in rows if r.id not in group_ids]

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [_process_one_with_sem(sem, row) for row in single_rows]
    tasks += [_process_group_chunk_with_sem(sem, chunk)
              for chunk in _chunk_group_rows(group_rows)]
    nested_results = await asyncio.gather(*tasks, return_exceptions=False)
    results = [item for sub in nested_results for item in sub]

    src_by_id = {r.id: r.source for r in rows}
    processed = 0
    llm_exhausted = 0
    emb_writes: list[tuple[int, list[float]]] = []  # → event_embeddings upsert
    rel_candidates: list[tuple[int, str]] = []       # → rel-extract после коммита триажа
    async with get_session() as s:
        for event_id, status, metadata, error in results:
            embedding = embeddings_by_id.get(event_id)
            if embedding is not None and status in ("done", "error"):
                emb_writes.append((event_id, embedding))
            if status == "pending":
                # LLM пул занят — вернём в pending, освобождаем triage_started_at
                await s.execute(
                    update(EventRow).where(EventRow.id == event_id).values(
                        triage_status="pending",
                        triage_started_at=None,
                    )
                )
                llm_exhausted += 1
            elif status == "done":
                await s.execute(
                    update(EventRow).where(EventRow.id == event_id).values(
                        triage_status="done",
                        triage_metadata=metadata,
                        importance=metadata.get("importance") if metadata else None,
                        nature=metadata.get("nature") if metadata else None,
                        project=metadata.get("project") if metadata else None,
                        ready_subtype=metadata.get("ready_subtype") if metadata else None,
                        triage_started_at=None,
                    )
                )
                processed += 1
                # Собираем кандидатов на rel-extract — запускаем ПОСЛЕ коммита
                # триажа (иначе фоновая задача читает событие до записи nature/
                # project). Только для high-signal событий.
                if metadata and metadata.get("importance", 0) >= 3:
                    row = next((r for r in rows if r.id == event_id), None)
                    if row and row.content_text:
                        rel_candidates.append((event_id, row.content_text))
            else:  # error
                # nature детерминируема по source даже без LLM
                err_nature = NATURE_BY_SOURCE.get(
                    src_by_id.get(event_id, ""), "world_event")
                await s.execute(
                    update(EventRow).where(EventRow.id == event_id).values(
                        triage_status="error",
                        triage_error=error,
                        nature=err_nature,
                        triage_started_at=None,
                    )
                )

    # Эмбеддинги — ОТДЕЛЬНОЙ транзакцией после коммита статусов: одно битое
    # событие в event_embeddings не должно откатывать triage_status всего батча
    # (иначе события зависают в processing до watchdog). Savepoint на строку.
    if emb_writes:
        async with get_session() as s:
            for eid, emb in emb_writes:
                try:
                    async with s.begin_nested():
                        await s.execute(text("""
                            INSERT INTO event_embeddings (event_id, embedding)
                            VALUES (:eid, CAST(:emb AS jsonb))
                            ON CONFLICT (event_id) DO UPDATE SET embedding = EXCLUDED.embedding
                        """), {"eid": eid, "emb": json.dumps(emb)})
                except Exception as e:
                    log.warning("embedding upsert failed event=%s: %s", eid, e)

    # Rel-extract — после коммита триажа, со ссылкой в _bg_tasks (иначе задачу
    # может собрать GC и связи молча потеряются).
    for eid, body in rel_candidates:
        t = asyncio.create_task(_safe_rel_extract(eid, body))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)

    # Детерминированный оверрайд project по папкам/аккаунтам — своя
    # транзакция, см. project_override.py.
    await apply_project_override([r.id for r in rows])

    log.info("[%s] processed: %d done, %d exhausted, %d errors",
             WORKER_ID, processed, llm_exhausted, len(rows) - processed - llm_exhausted)

    if PACE_BETWEEN_S > 0:
        await asyncio.sleep(PACE_BETWEEN_S)
    return processed


async def main_loop() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("[%s] brain-triage worker started, poll=%ss batch=%s concurrency=%s",
             WORKER_ID, POLL_INTERVAL_S, BATCH_SIZE, CONCURRENCY)

    asyncio.create_task(_watchdog_loop())
    asyncio.create_task(_retry_failed_loop())

    while True:
        try:
            n = await process_pending()
            if n == 0:
                await asyncio.sleep(POLL_INTERVAL_S)
        except Exception as e:
            log.exception("Outer loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_S * 2)


if __name__ == "__main__":
    asyncio.run(main_loop())

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
from vera_shared.control import is_backfill_paused, reserve_backfill_allowance
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.media_policy import should_extract_relations

from brain_triage.background_loops import (
    _safe_rel_extract,
    start_background_loops,
    track,
)
from brain_triage.claim import _chunk_group_rows, _claim_batch, chat_kind
from brain_triage.concurrency import (
    BATCH_MISS_ERROR,
    _process_group_chunk_with_sem,
    _process_one_with_sem,
)
from brain_triage.config import (
    BATCH_SIZE,
    CONCURRENCY,
    PACE_BETWEEN_S,
    POLL_INTERVAL_S,
    REL_EXTRACT_MIN_IMPORTANCE,
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
    # Even-tempo rate limit: атомарная резервация — реплики не могут вместе
    # превысить минутный бюджет (старый read-then-claim гонялся).
    granted = await reserve_backfill_allowance(BATCH_SIZE)
    if granted is not None and granted <= 0:
        return 0   # rate reached — main loop sleeps, recheck next cycle
    batch = BATCH_SIZE if granted is None else granted
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
    # strict=False сознательно: брокер может вернуть меньше векторов, чем
    # запрошено. Тогда часть событий останется без эмбеддинга (None) и будет
    # доэмбеждена позже — это лучше, чем уронить весь батч триажа.
    for pos, vec in zip(embed_idx, embed_vectors, strict=False):
        embeddings_by_id[rows[pos].id] = vec

    # Групповые telegram-сообщения (супергруппы + легаси Chat) батчатся по
    # TRIAGE_GROUP_BATCH_SIZE в один LLM-вызов — короткие тексты, экономия
    # call-budget под rate limiter. Каналы/личка/остальные источники — как
    # раньше, по одному, каждое сообщение разбирается отдельно.
    # Событие, которое LLM уже опустила в групповом ответе (batch-miss),
    # ретраим одиночно — иначе оно может выпадать из батча вечно.
    group_ids = {r.id for r in rows
                 if r.source == "telegram" and chat_kind(r) == "group"
                 and (r.triage_error or "") != BATCH_MISS_ERROR}
    group_rows = [r for r in rows if r.id in group_ids]
    single_rows = [r for r in rows if r.id not in group_ids]

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [_process_one_with_sem(sem, row) for row in single_rows]
    tasks += [_process_group_chunk_with_sem(sem, chunk)
              for chunk in _chunk_group_rows(group_rows)]
    nested_results = await asyncio.gather(*tasks, return_exceptions=False)
    results = [item for sub in nested_results for item in sub]

    src_by_id = {r.id: r.source for r in rows}
    # Fence: финальный UPDATE матчит только НАШ claim (triage_started_at из
    # RETURNING). Если watchdog вернул событие в pending (и его уже забрала
    # другая реплика) — обработка длилась > STUCK_AFTER_S — наш стейл-результат
    # не затирает чужой; 0 строк = молча уступаем.
    started_by_id = {r.id: r.triage_started_at for r in rows}
    processed = 0
    llm_exhausted = 0
    fenced_out = 0
    emb_writes: list[tuple[int, list[float]]] = []  # → event_embeddings upsert
    rel_candidates: list[tuple[int, str]] = []       # → rel-extract после коммита триажа
    async with get_session() as s:
        for event_id, status, metadata, error in results:
            fence = (EventRow.id == event_id,
                     EventRow.triage_started_at == started_by_id[event_id])
            embedding = embeddings_by_id.get(event_id)
            if embedding is not None and status in ("done", "error"):
                emb_writes.append((event_id, embedding))
            if status == "pending":
                # LLM пул занят / batch-miss — вернём в pending; ошибку пишем,
                # чтобы batch-miss ретраился одиночно (см. group_ids выше)
                res = await s.execute(
                    update(EventRow).where(*fence).values(
                        triage_status="pending",
                        triage_started_at=None,
                        triage_error=error,
                    )
                )
                llm_exhausted += 1
            elif status == "done":
                res = await s.execute(
                    update(EventRow).where(*fence).values(
                        triage_status="done",
                        triage_metadata=metadata,
                        importance=metadata.get("importance") if metadata else None,
                        nature=metadata.get("nature") if metadata else None,
                        project=metadata.get("project") if metadata else None,
                        ready_subtype=metadata.get("ready_subtype") if metadata else None,
                        triage_started_at=None,
                        triage_error=None,
                    )
                )
                processed += 1
                # Собираем кандидатов на rel-extract — запускаем ПОСЛЕ коммита
                # триажа (иначе фоновая задача читает событие до записи nature/
                # project). Только для high-signal событий (шкала 0-100, порог
                # в config.py) и только если наш результат реально записался
                # (не отфенсен).
                if ((res.rowcount or 0) > 0 and metadata
                        and metadata.get("importance", 0) >= REL_EXTRACT_MIN_IMPORTANCE):
                    row = next((r for r in rows if r.id == event_id), None)
                    if row and row.content_text and should_extract_relations(row.metadata_):
                        rel_candidates.append((event_id, row.content_text))
            else:  # error
                # nature детерминируема по source даже без LLM
                err_nature = NATURE_BY_SOURCE.get(
                    src_by_id.get(event_id, ""), "world_event")
                res = await s.execute(
                    update(EventRow).where(*fence).values(
                        triage_status="error",
                        triage_error=error,
                        nature=err_nature,
                        triage_started_at=None,
                    )
                )
            if (res.rowcount or 0) == 0:
                fenced_out += 1
    if fenced_out:
        log.warning("[%s] %d stale results fenced out (watchdog re-pended mid-run)",
                    WORKER_ID, fenced_out)

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
        track(asyncio.create_task(_safe_rel_extract(eid, body)))

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

    start_background_loops()

    from vera_shared.llm.circuit import llm_cooldown_remaining_s

    from brain_triage.triage_calls import (
        TRIAGE_CAPABILITIES,
        resolve_triage_capability,
    )

    while True:
        try:
            # Circuit breaker: не клеймим события, только если капнуты ОБЕ
            # triage-ёмкости (chat:fast и бесплатный фолбэк chat:smart) —
            # иначе события ушли бы в error об заведомый отказ и жгли retry.
            # Пока жива хоть одна — работаем на ней. Спим до ближайшего
            # восстановления кусками ≤60с.
            if await resolve_triage_capability() is None:
                cds = [await llm_cooldown_remaining_s(c) for c in TRIAGE_CAPABILITIES]
                soonest = min(cds)
                log.info("[%s] LLM circuit open (all triage caps; %.0f min left) "
                         "— triage idle", WORKER_ID, soonest / 60)
                await asyncio.sleep(min(soonest, 60))
                continue
            n = await process_pending()
            if n == 0:
                await asyncio.sleep(POLL_INTERVAL_S)
        except Exception as e:
            log.exception("Outer loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_S * 2)


if __name__ == "__main__":
    asyncio.run(main_loop())

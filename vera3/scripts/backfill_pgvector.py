"""Перелить эмбеддинги из JSONB в halfvec, построить индекс, проверить recall.

    python scripts/backfill_pgvector.py --status           # сколько осталось, индекс
    python scripts/backfill_pgvector.py [--batch 1000]      # перелить хвост
    python scripts/backfill_pgvector.py --index             # ANN-индекс CONCURRENTLY
    python scripts/backfill_pgvector.py --recall 30         # ANN против точного перебора

Почему не в миграции 030: 3.6 ГБ одной транзакцией заблокировали бы таблицу
и раздули WAL. Здесь порции по 1000 строк, каждая — своя транзакция.

Идём по первичному ключу (`event_id > :after`), а не `WHERE embedding_vec IS
NULL LIMIT n`: без индекса по NULL такой запрос на каждой порции заново
просматривает уже перелитое начало таблицы — квадратичная работа на 444 тыс.
строк. Перезапуск безопасен: UPDATE трогает только `embedding_vec IS NULL`.

Разбор JSON — в Postgres (`embedding::text` → halfvec), 7 КБ на строку не
гоняются через сеть. Строки, которые перелить нельзя (на 2026-09-13 — 2496
JSON `null`), пропускаются и остаются как есть: раньше скрипт их УДАЛЯЛ, а
удаление данных прода — не дело скрипта миграции.

Порядок на проде — docs/deploy-ops.md, «pgvector: накат и откат».
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Any

from sqlalchemy import text
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.vectors import (
    ANN_INDEX,
    ANN_SETTINGS_SQL,
    VEC_DIMS,
    VEC_TYPE,
    ann_candidates_sql,
    ann_index_sql,
    ann_settings_params,
)

log = logging.getLogger("backfill-pgvector")

#: Граф HNSW по битам 444 тыс. строк — ~0.2 ГБ; 192 МБ плюс shared_buffers
#: 128 МБ укладываются в mem_limit 768m контейнера. Не влезет — pgvector не
#: падает, а достраивает медленнее на диске (NOTICE в логе). Параллельные
#: воркеры выключены: их граф живёт в /dev/shm (shm_size 256mb).
INDEX_MEM_MB = 192

#: CASE, а не AND: порядок вычисления AND в SQL не гарантирован, а
#: jsonb_array_length на JSON `null` падает и уронил бы всю порцию.
_FILLABLE = ("CASE WHEN jsonb_typeof(embedding) = 'array'"
             " THEN jsonb_array_length(embedding) = :dims ELSE FALSE END")


def index_build_statements(dims: int, mem_mb: int) -> list[str]:
    return [
        f"SET maintenance_work_mem = '{int(mem_mb)}MB'",
        "SET max_parallel_maintenance_workers = 0",
        ann_index_sql(dims),
    ]


def recall_at_k(exact: list[int], approx: list[int]) -> float:
    return len(set(exact) & set(approx)) / len(exact) if exact else 1.0


async def fill_batch(after: int, size: int, dims: int) -> tuple[int | None, int]:
    """(последний просмотренный event_id или None в конце, сколько залито)."""
    async with get_session() as s:
        ids = list((await s.execute(text("""
            SELECT event_id FROM event_embeddings
            WHERE event_id > :after ORDER BY event_id LIMIT :n
        """), {"after": after, "n": size})).scalars())
        if not ids:
            return None, 0
        filled = (await s.execute(text(f"""
            UPDATE event_embeddings
            SET embedding_vec = CAST(embedding::text AS {VEC_TYPE}({dims}))
            WHERE event_id = ANY(:ids) AND embedding_vec IS NULL AND {_FILLABLE}
        """), {"ids": ids, "dims": dims})).rowcount
    return ids[-1], filled


async def status(dims: int) -> dict[str, Any]:
    async with get_session() as s:
        total, filled = (await s.execute(text(
            "SELECT COUNT(*), COUNT(embedding_vec) FROM event_embeddings"))).one()
        unfillable = (await s.execute(text(f"""
            SELECT COUNT(*) FROM event_embeddings
            WHERE embedding_vec IS NULL AND NOT {_FILLABLE}
        """), {"dims": dims})).scalar_one()
        index = (await s.execute(text("""
            SELECT i.indisvalid, pg_size_pretty(pg_relation_size(c.oid))
            FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = :name
        """), {"name": ANN_INDEX})).first()
    return {"total": total, "filled": filled, "unfillable": unfillable,
            "remaining": total - filled - unfillable,
            "index": "нет" if index is None
            else f"{'валиден' if index[0] else 'НЕВАЛИДЕН'}, {index[1]}"}


async def build_index(dims: int, mem_mb: int) -> None:
    """CONCURRENTLY — запись не блокируется. Прерванная постройка оставляет
    НЕВАЛИДНЫЙ индекс, и `IF NOT EXISTS` молча счёл бы его готовым, поэтому
    такой сначала сносим."""
    engine = await init_engine()
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        valid = (await conn.execute(text("""
            SELECT i.indisvalid FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name
        """), {"name": ANN_INDEX})).scalar_one_or_none()
        if valid is False:
            log.warning("индекс %s невалиден — пересоздаю", ANN_INDEX)
            await conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {ANN_INDEX}"))
        started = time.monotonic()
        for stmt in index_build_statements(dims, mem_mb):
            await conn.execute(text(stmt))
    log.info("индекс готов за %.0f с", time.monotonic() - started)


async def recall(samples: int, oversample: int, dims: int, k: int = 10) -> float:
    """Средний recall@k конвейера brain-search (биты → пересчёт halfvec)
    против точного перебора по всему корпусу. Запросы — векторы случайных
    событий, само событие из обоих ответов исключено."""
    async with get_session() as s:
        queries = (await s.execute(text("""
            SELECT event_id, embedding_vec::text FROM event_embeddings
            WHERE embedding_vec IS NOT NULL ORDER BY random() LIMIT :n
        """), {"n": samples})).all()
    scores: list[float] = []
    for self_id, q in queries:
        async with get_session() as s:
            exact = list((await s.execute(text(f"""
                SELECT event_id FROM event_embeddings
                WHERE embedding_vec IS NOT NULL AND event_id <> :self
                ORDER BY embedding_vec <=> CAST(:q AS {VEC_TYPE}({dims})) LIMIT :k
            """), {"self": self_id, "q": q, "k": k})).scalars())
            await s.execute(ANN_SETTINGS_SQL, ann_settings_params(oversample))
            approx = list((await s.execute(
                text(ann_candidates_sql(dims=dims, where="ee.event_id <> :self")),
                {"self": self_id, "q": q, "ann_k": oversample, "ann_top": k},
            )).scalars())
        scores.append(recall_at_k(exact, approx))
    return sum(scores) / len(scores) if scores else 0.0


async def backfill(size: int, dims: int, pause_s: float) -> int:
    after, moved, started = 0, 0, time.monotonic()
    while True:
        last, n = await fill_batch(after, size, dims)
        if last is None:
            return moved
        after, moved = last, moved + n
        rate = moved / max(time.monotonic() - started, 1e-9)
        log.info("event_id ≤ %d: залито %d (%.0f строк/с)", after, moved, rate)
        if pause_s:
            # не насос: даём триажу и поиску дисковое окно между порциями
            await asyncio.sleep(pause_s)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--pause", type=float, default=0.2)
    ap.add_argument("--dims", type=int, default=VEC_DIMS)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--index-mem-mb", type=int, default=INDEX_MEM_MB)
    ap.add_argument("--recall", type=int, metavar="N", default=0)
    ap.add_argument("--oversample", type=int, default=1000)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    await init_engine()

    if args.index:
        await build_index(args.dims, args.index_mem_mb)
    elif args.recall:
        r = await recall(args.recall, args.oversample, args.dims)
        log.info("recall@10 = %.3f (%d запросов, oversample %d)",
                 r, args.recall, args.oversample)
    elif not args.status:
        log.info("залито за прогон: %d", await backfill(args.batch, args.dims, args.pause))
    log.info("статус: %s", await status(args.dims))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

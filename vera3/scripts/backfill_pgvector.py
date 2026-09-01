"""Перелить эмбеддинги из JSONB в колонку vector. Батчами, идемпотентно.

    python scripts/backfill_pgvector.py              # весь хвост
    python scripts/backfill_pgvector.py --batch 2000 # размер порции
    python scripts/backfill_pgvector.py --index      # построить HNSW и выйти

Почему не в миграции 030: 3.6 ГБ одной транзакцией заблокировали бы таблицу
и раздули WAL. Здесь порции по 1000 строк, каждая — своя транзакция; можно
прервать в любой момент и запустить снова, работа не потеряется (условие
`embedding_vec IS NULL` само сужается).

Порядок на проде:
  1. накатить 030 (колонка появляется пустой — рабочее состояние, код
     читает JSONB);
  2. деплой кода (он уже умеет обе колонки и пишет в обе);
  3. этот скрипт до «осталось 0»;
  4. `--index` — HNSW строится CONCURRENTLY, не блокируя запись;
  5. только после этого имеет смысл думать про DROP старой колонки —
     отдельной миграцией и не в тот же день.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time

from sqlalchemy import text
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.vectors import as_pg_vector

log = logging.getLogger("backfill-pgvector")

#: lists/m/ef_construction по умолчанию pgvector — на 400 тыс. строк
#: разумно; тюнинг имеет смысл только после замера recall на живых запросах.
INDEX_SQL = """
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_event_embeddings_vec
    ON event_embeddings USING hnsw (embedding_vec vector_cosine_ops)
"""


async def remaining() -> int:
    async with get_session() as s:
        return (await s.execute(text(
            "SELECT COUNT(*) FROM event_embeddings WHERE embedding_vec IS NULL"
        ))).scalar_one()


async def copy_batch(size: int) -> int:
    """Одна порция. Возвращает, сколько строк перелито."""
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT event_id, embedding FROM event_embeddings
            WHERE embedding_vec IS NULL
            LIMIT :n
        """), {"n": size})).all()
        if not rows:
            return 0
        done = 0
        for event_id, raw in rows:
            # JSONB приходит уже разобранным (list), но asyncpg отдаёт строкой
            # то, что в колонке лежит СТРОКОЙ json'а — и это не обязательно
            # валидный список. Разбор обязан быть безопасным: одна битая
            # строка не должна ронять весь батч.
            try:
                emb = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                emb = None
            if not isinstance(emb, list) or not emb:
                # Битая строка: помечать нечем, но и висеть в выборке вечно
                # она не должна — иначе цикл не закончится никогда.
                log.warning("event %s: эмбеддинг не список, пропускаю", event_id)
                await s.execute(text(
                    "DELETE FROM event_embeddings WHERE event_id = :e"
                ), {"e": event_id})
                continue
            await s.execute(text("""
                UPDATE event_embeddings
                SET embedding_vec = CAST(:v AS vector)
                WHERE event_id = :e
            """), {"e": event_id, "v": as_pg_vector(emb)})
            done += 1
        return done


async def build_index() -> None:
    """HNSW отдельно и CONCURRENTLY: на пустой колонке индекс бесполезен, а
    на залитой строится долго и не должен блокировать запись."""
    engine = await init_engine()
    # CONCURRENTLY нельзя внутри транзакции — нужен autocommit
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        log.info("строю HNSW (может занять минуты)…")
        await conn.execute(text(INDEX_SQL))
    log.info("индекс готов")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--index", action="store_true",
                    help="построить HNSW и выйти")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    await init_engine()

    if args.index:
        await build_index()
        return 0

    left = await remaining()
    log.info("осталось перелить: %d", left)
    moved = 0
    started = time.monotonic()
    while True:
        n = await copy_batch(args.batch)
        if n == 0:
            break
        moved += n
        rate = moved / max(time.monotonic() - started, 1e-9)
        log.info("перелито %d/%d (%.0f строк/с)", moved, left, rate)

    left_after = await remaining()
    log.info("готово: перелито %d, осталось %d", moved, left_after)
    if left_after == 0:
        log.info("теперь можно строить индекс: --index")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

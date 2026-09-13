"""Векторы кусков длинных событий — `event_chunk_embeddings` (миграция 032).

Один вектор на событие размывает длинный текст: письмо на 20 тыс. символов с
одним абзацем про аренду по косинусу похоже «на всё письмо», а не на аренду.
Поэтому у события длиннее `text_chunks.CHUNK_THRESHOLD` есть ещё куски, и
поиск берёт лучший косинус из вектора события и векторов его кусков.

Короткие события сюда не пишутся: их вектор уже лежит в event_embeddings.
Таблица только halfvec — она новее миграции 030, JSONB-наследия у неё нет.
Код обязан работать и до наката 032: тогда кусков просто нет.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from vera_shared.db import vectors
from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

CHUNK_TABLE = "event_chunk_embeddings"
CHUNK_ANN_INDEX = "ix_event_chunk_embeddings_vec_bq"

_has_table: bool | None = None
_has_ann: bool | None = None


def forget_chunk_capability() -> None:
    global _has_table, _has_ann
    _has_table = None
    _has_ann = None


async def chunk_table_available() -> bool:
    """Накачена ли 032. Кэшируется на процесс, как vectors.vector_column_available."""
    global _has_table
    if _has_table is not None:
        return _has_table
    try:
        async with get_session() as s:
            found = (await s.execute(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
                {"t": CHUNK_TABLE})).scalar_one_or_none()
        _has_table = found is not None
    except Exception as e:  # noqa: BLE001 — SQLite/каталог недоступен: без кусков
        log.debug("проверка таблицы %s не удалась: %s", CHUNK_TABLE, e)
        _has_table = False
    log.info("эмбеддинги: куски длинных событий %s",
             "доступны" if _has_table else "не накачены (032)")
    return _has_table


async def chunk_ann_available() -> bool:
    global _has_ann
    if _has_ann is not None:
        return _has_ann
    _has_ann = (await chunk_table_available()
                and await vectors.index_is_valid(CHUNK_ANN_INDEX))
    return _has_ann


def chunk_schema_sql(dims: int) -> list[str]:
    """DDL таблицы и индекса — один источник для миграции 032 (её текст
    сверяется с этим в тестах) и интеграционных тестов на dims=3.
    Индекс без CONCURRENTLY: таблица создаётся пустой, строить нечего."""
    return [
        f"""CREATE TABLE IF NOT EXISTS {CHUNK_TABLE} (
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    chunk_no SMALLINT NOT NULL,
    embedding_vec {vectors.VEC_TYPE}({dims}) NOT NULL,
    PRIMARY KEY (event_id, chunk_no)
)""",
        f"ALTER TABLE {CHUNK_TABLE} ALTER COLUMN embedding_vec SET STORAGE PLAIN",
        vectors.ann_index_sql(dims, table=CHUNK_TABLE, index=CHUNK_ANN_INDEX,
                              concurrently=False),
    ]


def chunk_candidates_sql(*, dims: int, where: str) -> str:
    return vectors.ann_candidates_sql(dims=dims, where=where, join_events=True,
                                      table=CHUNK_TABLE)


def chunk_delete_sql() -> Any:
    return text(f"DELETE FROM {CHUNK_TABLE} WHERE event_id = ANY(:ids)")


def chunk_insert_sql() -> Any:
    return text(f"""
        INSERT INTO {CHUNK_TABLE} (event_id, chunk_no, embedding_vec)
        VALUES (:eid, :no, CAST(:vec AS {vectors.VEC_TYPE}))
    """)


async def replace_event_chunks(event_id: int,
                               embeddings: list[list[float]]) -> None:
    """Куски события целиком заменяются: сессию Claude дописывают, и старый
    хвост кусков не должен пережить новый текст."""
    async with get_session() as s:
        await s.execute(chunk_delete_sql(), {"ids": [event_id]})
        for no, emb in enumerate(embeddings):
            await s.execute(chunk_insert_sql(), {
                "eid": event_id, "no": no, "vec": vectors.as_pg_vector(emb)})


async def drop_event_chunks(event_ids: list[int]) -> None:
    """Событие стало коротким (перезаписанная выжимка) — куски больше не его."""
    if not event_ids:
        return
    async with get_session() as s:
        await s.execute(chunk_delete_sql(), {"ids": event_ids})

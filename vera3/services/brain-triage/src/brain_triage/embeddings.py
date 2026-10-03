"""Запись векторов событий в event_embeddings — общий путь триажа и reembed."""
from __future__ import annotations

import logging

from vera_shared.db.engine import get_session
from vera_shared.db.vectors import embedding_upsert, vector_column_available

log = logging.getLogger(__name__)


async def write_embeddings(pairs: list[tuple[int, list[float]]]) -> int:
    """Upsert (event_id, вектор) во ВСЕ колонки, что есть — см.
    vectors.embedding_upsert. Возвращает число записанных строк."""
    to_vec = await vector_column_available()
    written = 0
    async with get_session() as s:
        for eid, emb in pairs:
            sql, params = embedding_upsert(eid, emb, to_vec)
            try:
                # Savepoint на строку: одно битое событие не должно
                # откатывать весь батч эмбеддингов.
                async with s.begin_nested():
                    await s.execute(sql, params)
                written += 1
            except Exception as e:
                log.warning("embedding upsert failed event=%s: %s", eid, e)
    return written

"""Где лежат эмбеддинги: колонка `vector` или JSONB — и как это пережить.

Миграция 030 добавляет `event_embeddings.embedding_vec vector(1024)` рядом
со старым JSONB-полем. Заливка 3.6 ГБ идёт батчами отдельным скриптом и
занимает время, поэтому код обязан работать в ЛЮБОЙ точке перехода:

* колонки ещё нет (миграция не накачена) → читаем JSONB;
* колонка есть, но пустая → читаем JSONB;
* колонка залита частично → у строки берём то, что есть;
* всё залито → читаем вектор, ANN-индекс делает отбор.

Проверка наличия колонки кэшируется на процесс: она не меняется в рантайме,
а спрашивать каталог на каждый запрос — тот же класс расточительства, что и
кулдаун LLM (см. llm/circuit.py).

На SQLite (тесты) колонки нет никогда, и это штатная ветка, а не заглушка.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

_has_vector: bool | None = None


def forget_capability() -> None:
    """Сбросить кэш — для тестов и после наката миграции без рестарта."""
    global _has_vector
    _has_vector = None


async def vector_column_available() -> bool:
    """Есть ли `event_embeddings.embedding_vec`. Кэшируется на процесс."""
    global _has_vector
    if _has_vector is not None:
        return _has_vector
    try:
        async with get_session() as s:
            found = (await s.execute(text("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'event_embeddings'
                  AND column_name = 'embedding_vec'
            """))).scalar_one_or_none()
        _has_vector = found is not None
    except Exception as e:  # noqa: BLE001 — SQLite/каталог недоступен: живём на JSONB
        log.debug("проверка колонки embedding_vec не удалась: %s", e)
        _has_vector = False
    if _has_vector:
        log.info("эмбеддинги: колонка vector доступна, косинус считает Postgres")
    else:
        log.info("эмбеддинги: колонки vector нет, косинус считается на Python")
    return _has_vector


def as_pg_vector(embedding: list[float]) -> str:
    """Литерал pgvector: '[0.1,0.2]'. Драйверу отдаём строкой и кастуем в SQL —
    так не нужен pgvector-адаптер в asyncpg."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"

"""Где лежат эмбеддинги: колонка `halfvec` или JSONB — и как это пережить.

Миграция 030 добавляет `event_embeddings.embedding_vec halfvec(1024)` рядом
со старым JSONB-полем. Заливка 3.6 ГБ идёт батчами отдельным скриптом и
занимает время, поэтому код обязан работать в ЛЮБОЙ точке перехода:

* колонки ещё нет (миграция не накачена) → читаем JSONB;
* колонка есть, но пустая → читаем JSONB;
* колонка залита частично → у строки берём то, что есть;
* залито и построен индекс → ANN-отбор (`ann_candidates_sql`).

Проверки каталога кэшируются на процесс: они не меняются в рантайме,
а спрашивать каталог на каждый запрос — тот же класс расточительства, что и
кулдаун LLM (см. llm/circuit.py).

На SQLite (тесты) колонки нет никогда, и это штатная ветка, а не заглушка.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

#: Размерность voyage-4 (scripts/reembed_voyage4.py). В выражении индекса
#: она зашита (`::bit(1024)`), и запрос обязан повторить выражение дословно,
#: иначе планировщик индекс не узнает. Интеграционные тесты подменяют на 3.
VEC_DIMS = 1024
#: halfvec, а не vector: float16 вдвое компактнее (2 КБ против 4 КБ на
#: строку), а ошибка косинуса на выборке прода — 4e-5 (замер 2026-09-13),
#: на порядки ниже любого нашего порога.
VEC_TYPE = "halfvec"
ANN_INDEX = "ix_event_embeddings_vec_bq"

_has_vector: bool | None = None
_has_ann: bool | None = None


def forget_capability() -> None:
    """Сбросить кэш — для тестов и после наката миграции без рестарта."""
    global _has_vector, _has_ann
    _has_vector = None
    _has_ann = None


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


async def ann_index_available() -> bool:
    """Построен ли и ВАЛИДЕН ли ANN-индекс. Кэшируется на процесс.

    Индекс — выключатель ANN-отбора: без него «ближайшие ко всему корпусу»
    стали бы seq scan'ом ~1 ГБ halfvec на каждый поиск. Недостроенный
    CONCURRENTLY индекс остаётся в каталоге с indisvalid=false — его не
    считаем. Включение после постройки — рестарт brain-search."""
    global _has_ann
    if _has_ann is not None:
        return _has_ann
    if not await vector_column_available():
        _has_ann = False
        return False
    async with get_session() as s:
        found = (await s.execute(text("""
            SELECT i.indisvalid FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = :name
        """), {"name": ANN_INDEX})).scalar_one_or_none()
    _has_ann = bool(found)
    log.info("эмбеддинги: ANN-индекс %s", "доступен" if _has_ann else "не построен")
    return _has_ann


def bq(expr: str, dims: int) -> str:
    """Бинарное квантование ровно в той форме, что в выражении индекса."""
    return f"(binary_quantize({expr})::bit({dims}))"


def ann_index_sql(dims: int) -> str:
    """HNSW по знаку каждого измерения: 128 байт на строку вместо 2 КБ
    halfvec — индекс ~0.2 ГБ помещается в память контейнера 768m, а
    полноразмерный HNSW на halfvec был бы ~1 ГБ. Точность возвращает
    пересчёт кандидатов по halfvec в `ann_candidates_sql`."""
    return (f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {ANN_INDEX}"
            f" ON event_embeddings USING hnsw"
            f" ({bq('embedding_vec', dims)} bit_hamming_ops)")


def ann_candidates_sql(*, dims: int, where: str = "TRUE",
                       join_events: bool = False) -> str:
    """(event_id, sim) ближайших к :q. Параметры: :q (литерал as_pg_vector),
    :ann_k (сколько взять из индекса), :ann_top (сколько вернуть).

    Два шага: индекс по Хэммингу отдаёт :ann_k грубых кандидатов, затем они
    пересчитываются точным косинусом по halfvec. `where` стоит внутри
    индексного шага — вместе с hnsw.iterative_scan (`ANN_SETTINGS_SQL`)
    фильтр по времени/проекту не опустошает выдачу."""
    join = "JOIN events ON events.id = ee.event_id" if join_events else ""
    q = f"CAST(:q AS {VEC_TYPE}({dims}))"
    return f"""
        SELECT c.event_id, 1 - (c.embedding_vec <=> {q}) AS sim
        FROM (
            SELECT ee.event_id, ee.embedding_vec
            FROM event_embeddings ee {join}
            WHERE ee.embedding_vec IS NOT NULL AND ({where})
            ORDER BY {bq('ee.embedding_vec', dims)} <~> {bq(q, dims)}
            LIMIT :ann_k
        ) c
        ORDER BY c.embedding_vec <=> {q}
        LIMIT :ann_top
    """


#: ef_search обязан быть ≥ LIMIT индексного шага: по умолчанию он 40, и HNSW
#: молча вернул бы 40 строк вместо запрошенных сотен. relaxed_order —
#: pgvector ≥0.8 (на проде 0.8.2): при фильтре скан продолжается, пока LIMIT
#: не наберётся; порядок внутри грубого шага не важен, его чинит пересчёт.
ANN_SETTINGS_SQL = text(
    "SELECT set_config('hnsw.ef_search', :ef, true),"
    " set_config('hnsw.iterative_scan', 'relaxed_order', true)"
)


def ann_settings_params(ann_k: int) -> dict[str, str]:
    """ef_search в пределах pgvector: 1..1000."""
    return {"ef": str(min(max(ann_k, 40), 1000))}


def embedding_upsert(event_id: int, embedding: list[float],
                     with_vec: bool) -> tuple[Any, dict[str, Any]]:
    """(SQL, параметры) записи эмбеддинга во ВСЕ колонки, что есть. Пока идёт
    бэкфил, новое событие обязано попасть и в halfvec, и в JSONB — иначе оно
    окажется в дыре, которую бэкфил уже прошёл. Один источник для триажа и
    /v1/claude/remember: раньше remember писал только JSONB."""
    params: dict[str, Any] = {"eid": event_id, "emb": json.dumps(embedding)}
    if not with_vec:
        return text("""
            INSERT INTO event_embeddings (event_id, embedding)
            VALUES (:eid, CAST(:emb AS jsonb))
            ON CONFLICT (event_id) DO UPDATE SET embedding = EXCLUDED.embedding
        """), params
    params["vec"] = as_pg_vector(embedding)
    return text(f"""
        INSERT INTO event_embeddings (event_id, embedding, embedding_vec)
        VALUES (:eid, CAST(:emb AS jsonb), CAST(:vec AS {VEC_TYPE}))
        ON CONFLICT (event_id) DO UPDATE
          SET embedding = EXCLUDED.embedding,
              embedding_vec = EXCLUDED.embedding_vec
    """), params


def as_pg_vector(embedding: list[float]) -> str:
    """Литерал pgvector: '[0.1,0.2]'. Драйверу отдаём строкой и кастуем в SQL —
    так не нужен pgvector-адаптер в asyncpg."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"

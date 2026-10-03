"""Где лежат эмбеддинги: `event_embeddings.embedding_vec halfvec(1024)`.

Единственное хранилище вектора события. Старая JSONB-колонка `embedding`
(~6.3 КБ на строку, ~3 ГБ TOAST) снята миграциями 038/039: код её не читает и
не пишет, поэтому здесь нет ни проверок «есть ли колонка», ни запасных веток
на Python — они существовали только на время перехода.

На SQLite (юнит-тесты) колонки `embedding_vec` нет: ORM-модель её не знает,
SQL с векторами исполняется только в интеграционных тестах на Postgres+pgvector.
Индекс ANN — `ix_event_embeddings_vec_bq`; если его снесли, а brain-search не
перезапускали, запрос упирается в statement_timeout (`ANN_STATEMENT_TIMEOUT_MS`)
и поиск деградирует до полнотекста (`brain_search.ann.fetch_ann_rows`).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session

#: Размерность voyage-4 (scripts/reembed_voyage4.py). В выражении индекса
#: она зашита (`::bit(1024)`), и запрос обязан повторить то же выражение по
#: смыслу (колонка, функция, тип, размерность; квалификация таблицей не мешает —
#: планировщик сравнивает разобранное выражение, а не текст), иначе индекс не
#: будет использован. Интеграционные тесты подменяют на 3.
VEC_DIMS = 1024
#: halfvec, а не vector: float16 вдвое компактнее (2 КБ против 4 КБ на
#: строку), а ошибка косинуса на выборке прода — 4e-5 (замер 2026-09-13),
#: на порядки ниже любого нашего порога.
VEC_TYPE = "halfvec"
ANN_INDEX = "ix_event_embeddings_vec_bq"


async def index_is_valid(name: str) -> bool:
    """Есть ли индекс и валиден ли он (недостроенный CONCURRENTLY — нет)."""
    async with get_session() as s:
        found = (await s.execute(text("""
            SELECT i.indisvalid FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = :name
        """), {"name": name})).scalar_one_or_none()
    return bool(found)


def bq(expr: str, dims: int) -> str:
    """Бинарное квантование ровно в той форме, что в выражении индекса."""
    return f"(binary_quantize({expr})::bit({dims}))"


def ann_index_sql(dims: int, *, table: str = "event_embeddings",
                  index: str = ANN_INDEX, concurrently: bool = True) -> str:
    """HNSW по знаку каждого измерения: 128 байт на строку вместо 2 КБ
    halfvec — индекс ~0.2 ГБ помещается в память контейнера 768m, а
    полноразмерный HNSW на halfvec был бы ~1 ГБ. Точность возвращает
    пересчёт кандидатов по halfvec в `ann_candidates_sql`."""
    how = " CONCURRENTLY" if concurrently else ""
    return (f"CREATE INDEX{how} IF NOT EXISTS {index}"
            f" ON {table} USING hnsw"
            f" ({bq('embedding_vec', dims)} bit_hamming_ops)")


def ann_candidates_sql(*, dims: int, where: str = "TRUE",
                       join_events: bool = False,
                       table: str = "event_embeddings") -> str:
    """(event_id, sim) ближайших к :q. Параметры: :q (литерал as_pg_vector),
    :ann_k (сколько взять из индекса), :ann_top (сколько вернуть).

    Два шага: индекс по Хэммингу отдаёт :ann_k грубых кандидатов, затем они
    пересчитываются точным косинусом по halfvec. `where` стоит внутри
    индексного шага — вместе с hnsw.iterative_scan (`ANN_SETTINGS_SQL`)
    фильтр по времени/проекту не опустошает выдачу. `table` — любая таблица
    с (event_id, embedding_vec): у кусков (chunk_vectors) event_id повторяется."""
    join = "JOIN events ON events.id = ee.event_id" if join_events else ""
    q = f"CAST(:q AS {VEC_TYPE}({dims}))"
    return f"""
        SELECT c.event_id, 1 - (c.embedding_vec <=> {q}) AS sim
        FROM (
            SELECT ee.event_id, ee.embedding_vec
            FROM {table} ee {join}
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
    " set_config('hnsw.iterative_scan', 'relaxed_order', true),"
    " set_config('statement_timeout', :timeout, true)"
)
#: Потолок одного смыслового запроса. Штатно HNSW отвечает за доли секунды;
#: если индекс снесли, а brain-search не перезапустили, процесс по кэшу думает,
#: что индекс есть, и запрос ушёл бы seq scan'ом по ~1 ГБ halfvec в контейнере
#: на 768m — минуты на КАЖДЫЙ поиск (ревью 13.09.2026). Все настройки — только
#: на транзакцию (третий аргумент set_config = true), в пул не утекают.
ANN_STATEMENT_TIMEOUT_MS = 5000


#: Потолок ожидания блокировки в поиске: пока `VACUUM FULL event_embeddings`
#: держит ACCESS EXCLUSIVE, запрос с JOIN по таблице не должен висеть минутами.
LOCK_TIMEOUT_MS = 3000
LOCK_TIMEOUT_SQL = text("SELECT set_config('lock_timeout', :lock_timeout, true)")


def lock_timeout_params() -> dict[str, str]:
    return {"lock_timeout": str(LOCK_TIMEOUT_MS)}


def ann_settings_params(ann_k: int) -> dict[str, str]:
    """ef_search в пределах pgvector: 1..1000."""
    return {"ef": str(min(max(ann_k, 40), 1000)),
            "timeout": str(ANN_STATEMENT_TIMEOUT_MS)}


def embedding_upsert(event_id: int, embedding: list[float]) -> tuple[Any, dict[str, Any]]:
    """(SQL, параметры) записи вектора события. Один источник для триажа,
    reembed и /v1/claude/remember."""
    params: dict[str, Any] = {"eid": event_id, "vec": as_pg_vector(embedding)}
    return text(f"""
        INSERT INTO event_embeddings (event_id, embedding_vec)
        VALUES (:eid, CAST(:vec AS {VEC_TYPE}))
        ON CONFLICT (event_id) DO UPDATE SET embedding_vec = EXCLUDED.embedding_vec
    """), params


def as_pg_vector(embedding: list[float]) -> str:
    """Литерал pgvector: '[0.1,0.2]'. Драйверу отдаём строкой и кастуем в SQL —
    так не нужен pgvector-адаптер в asyncpg."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"

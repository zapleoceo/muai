-- Migration 032: векторы кусков длинных событий.
-- (031 зарезервирован под многоязычный FTS — параллельная ветка.)
--
-- ЗАЧЕМ. Один voyage-4 вектор на событие: длинное письмо или выжимка сессии
-- Claude в одном векторе размывается, а после 8000 символов не была
-- представлена вовсе. Замер прода 2026-09-13: 1741 событие длиннее 4000
-- символов (~9.8 тыс. кусков по 1500 с перекрытием 200), ~490 новых в месяц.
-- Короткие события сюда НЕ пишутся — их вектор остаётся в event_embeddings.
--
-- РАЗМЕР. ~10 тыс. строк × ~2.1 КБ halfvec (PLAIN, в строке — см. 030) ≈
-- 25 МБ + HNSW по битам ~2 МБ. Индекс строится здесь же, без CONCURRENTLY:
-- таблица создаётся пустой, строить нечего, блокировать некого.
--
-- ЗАВИСИМОСТЬ. Нужен тип halfvec — расширение vector (030). CREATE EXTENSION
-- повторён, чтобы порядок наката 030/032 не мог сломать эту миграцию.
--
-- DDL совпадает с vera_shared.db.chunk_vectors.chunk_schema_sql(1024) —
-- расхождение ловит tests/unit/test_text_chunks.py.
--
-- Откат: DROP TABLE event_chunk_embeddings; поиск и триаж перестанут его
-- видеть после рестарта brain-search и brain-triage (кэш возможностей).

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS event_chunk_embeddings (
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    chunk_no SMALLINT NOT NULL,
    embedding_vec halfvec(1024) NOT NULL,
    PRIMARY KEY (event_id, chunk_no)
);

ALTER TABLE event_chunk_embeddings ALTER COLUMN embedding_vec SET STORAGE PLAIN;

CREATE INDEX IF NOT EXISTS ix_event_chunk_embeddings_vec_bq ON event_chunk_embeddings USING hnsw ((binary_quantize(embedding_vec)::bit(1024)) bit_hamming_ops);

INSERT INTO schema_migrations (version, note)
VALUES ('032_event_chunk_embeddings',
        'event_chunk_embeddings halfvec(1024) + HNSW по binary_quantize; куски пишет триаж')
ON CONFLICT (version) DO NOTHING;

COMMIT;

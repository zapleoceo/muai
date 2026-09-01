-- Migration 030: эмбеддинги в колонку vector(1024) рядом с JSONB.
--
-- ЧТО НЕ ТАК СЕЙЧАС. Образ базы — pgvector/pgvector:pg16, VERA.md и
-- architecture.md обещают «Postgres + pgvector для эмбеддингов». По факту
-- расширение не создано ни разу, колонка объявлена JSONB (миграция 011),
-- ANN-индекса нет, а косинус считается циклом на Python в двух местах:
--   brain_search/scoring.py  — до 200 строк на запрос;
--   gateway/claude.py        — до 500 строк на КАЖДЫЙ /v1/claude/remember.
-- Это ~512 тыс. float, разбираемых из JSON-текста, и ~1.5 млн операций в
-- CPython на один вызов. Плюс место: event_embeddings — 3.6 ГБ, 66% всей
-- базы (см. scripts/vera-backup.sh). Как vector(1024) та же строка весит
-- 4 КБ вместо 15-20 КБ JSON-текста.
--
-- ЭТА МИГРАЦИЯ НИЧЕГО НЕ ЛОМАЕТ И НИЧЕГО НЕ ПЕРЕНОСИТ. Она только
-- добавляет пустую колонку и расширение. Данные заливает отдельный скрипт
-- батчами (scripts/backfill_pgvector.py) — 3.6 ГБ одной транзакцией
-- заблокировали бы таблицу и раздули WAL. Код на это время читает вектор
-- из `embedding_vec`, если он там есть, и из `embedding` (JSONB), если нет,
-- поэтому порядок «накатить → бэкфилить → включить» безопасен в любой точке.
--
-- Старая колонка НЕ удаляется здесь СОЗНАТЕЛЬНО: пока бэкфил не проверен на
-- живых данных, откат должен быть бесплатным. Удаление + VACUUM FULL —
-- отдельной миграцией, после того как `embedding_vec IS NULL` перестанет
-- находить строки.
--
-- Индекс HNSW строится ПОСЛЕ бэкфила, тоже отдельно: на пустой колонке он
-- бесполезен, а на 400 тыс. строк строится долго и его лучше пускать
-- CONCURRENTLY (см. хвост скрипта бэкфила).

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- 1024 — размерность voyage (scripts/reembed_voyage4.py). Колонка nullable:
-- до бэкфила она пуста у всех строк, и это рабочее состояние.
ALTER TABLE event_embeddings
    ADD COLUMN IF NOT EXISTS embedding_vec vector(1024);

INSERT INTO schema_migrations (version, note)
VALUES ('030_event_embeddings_pgvector',
        'CREATE EXTENSION vector + колонка embedding_vec; бэкфил и HNSW — отдельно')
ON CONFLICT (version) DO NOTHING;

COMMIT;

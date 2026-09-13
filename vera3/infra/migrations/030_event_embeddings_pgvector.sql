-- Migration 030: эмбеддинги в колонку halfvec(1024) рядом с JSONB.
--
-- ЧТО НЕ ТАК СЕЙЧАС. Образ базы — pgvector/pgvector:pg16 (на проде
-- расширение 0.8.2), но колонка объявлена JSONB (миграция 011), ANN-индекса
-- нет, а косинус считается циклом на Python в двух местах:
--   brain_search/scoring.py  — до 200 строк на запрос, отобранных ПОЛНОТЕКСТОМ,
--                              т.е. текст без общих слов не находится вовсе;
--   gateway/claude.py        — до 500 строк на КАЖДЫЙ /v1/claude/remember.
-- event_embeddings — 3.6 ГБ, 444 тыс. строк (замер 2026-09-13), 66% базы;
-- JSONB-строка весит ~7 КБ.
--
-- ПОЧЕМУ halfvec, А НЕ vector (исправлено 2026-09-13 до наката; первая
-- редакция объявляла vector(1024)). float16 — 2 КБ на строку вместо 4 КБ:
-- ~0.9 ГБ вместо ~1.8 ГБ на 444 тыс. строк. Ошибка косинуса на выборке
-- прода — 4e-5, ниже любого нашего порога (дедуп 0.92). halfvec есть в
-- pgvector с 0.7.
--
-- ПОЧЕМУ STORAGE PLAIN. У типов pgvector хранение EXTERNAL: строка halfvec
-- (2 КБ + заголовок) переваливает порог TOAST ~2 КБ и уехала бы в
-- toast-таблицу. Тогда точный пересчёт 400 кандидатов на каждый поиск —
-- это ещё ~800 чтений toast-индекса и чанков. 2 КБ спокойно помещаются в
-- 8-КБ страницу рядом с указателем на JSONB, так что держим вектор в строке.
-- На пустой колонке это изменение только каталога.
--
-- ЭТА МИГРАЦИЯ НИЧЕГО НЕ ЛОМАЕТ И НИЧЕГО НЕ ПЕРЕНОСИТ: расширение и пустая
-- nullable колонка. Данные — scripts/backfill_pgvector.py батчами (одна
-- транзакция на 3.6 ГБ заблокировала бы таблицу и раздула WAL). Индекс —
-- тот же скрипт `--index`, CONCURRENTLY, после бэкфила. Код в любой точке
-- читает вектор, если он есть, и JSONB, если нет.
--
-- Старая колонка НЕ удаляется СОЗНАТЕЛЬНО: откат должен быть бесплатным.
-- DROP + VACUUM FULL — отдельной миграцией, не в тот же день.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE event_embeddings
    ADD COLUMN IF NOT EXISTS embedding_vec halfvec(1024);

ALTER TABLE event_embeddings
    ALTER COLUMN embedding_vec SET STORAGE PLAIN;

INSERT INTO schema_migrations (version, note)
VALUES ('030_event_embeddings_pgvector',
        'CREATE EXTENSION vector + колонка embedding_vec halfvec(1024) PLAIN; бэкфил и индекс — отдельно')
ON CONFLICT (version) DO NOTHING;

COMMIT;

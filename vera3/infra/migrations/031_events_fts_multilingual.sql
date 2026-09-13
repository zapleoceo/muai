-- Migration 031: второй полнотекстовый индекс по events — конфигурация indonesian.
--
-- Поиск стоял на одной `russian` (замер 13.09.2026, 445 тыс. событий, из них
-- ~115 тыс. с украинскими буквами и ~31 тыс. без кириллицы вовсе — письма и
-- чаты IT STEP Jakarta). `russian` сама стеммит английский (латиница там
-- идёт в english_stem) и префиксом ловит украинский, но индонезийский не
-- стеммит: на 5% выборке pendaftaran:* 2 против 15, pembayaran:* 4 против 9.
-- `indonesian` стеммит латиницу по-индонезийски, а кириллицу оставляет
-- точным токеном (= simple) — это же и лучший доступный вариант для
-- украинского: hunspell uk в образе pgvector/pgvector:pg16 нет.
-- Запрос — OR двух условий (brain_search/fts.py), план — BitmapOr.
--
-- Почему выражение, а не колонка: generated column или ALTER … ADD COLUMN с
-- бэкфилом переписывает/раздувает events (700 МБ heap) на горячей таблице.
-- Индекс по выражению строится CONCURRENTLY и таблицу не трогает.
-- Почему отдельный индекс, а не один по `russian || indonesian`: тот ~1.58×
-- русского (~196 МБ) и строился бы рядом со старым, а этот ~1.19× (~150 МБ
-- по отношению лексем на выборке; русский сейчас 124 МБ), и старый остаётся.
--
-- ix_events_fts_russian на проде уже есть (ставился руками мимо миграций);
-- здесь он фиксируется для чистых баз, на проде строка — no-op.
--
-- CONCURRENTLY — без BEGIN/COMMIT (в транзакции запрещён). Упавшее построение
-- оставляет INVALID-индекс, который IF NOT EXISTS при повторе пропустит;
-- apply_migration.sh это ловит и в учёт не пишет. Тогда:
--   DROP INDEX CONCURRENTLY IF EXISTS ix_events_fts_indonesian;  и накатить заново.
-- Откат: DROP INDEX CONCURRENTLY IF EXISTS ix_events_fts_indonesian; код без
-- индекса работает (второе условие OR уйдёт в seq scan — медленно, но верно),
-- поэтому индекс катить ДО деплоя кода.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_fts_russian
    ON events USING gin (to_tsvector('russian', content_text));

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_fts_indonesian
    ON events USING gin (to_tsvector('indonesian', content_text));

INSERT INTO schema_migrations (version, note)
VALUES ('031_events_fts_multilingual', 'ix_events_fts_indonesian: FTS по индонезийскому и точным токенам')
ON CONFLICT (version) DO NOTHING;

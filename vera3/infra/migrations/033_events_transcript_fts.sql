-- Migration 033: дословная стенограмма в полнотекстовом поиске.
--
-- Замер 17.09.2026 на проде. Событие 483722 — созвон 118 минут, 3787 реплик,
-- 151 237 символов дословно и 8 287 символов выжимки в content_text. Из 2786
-- различных слов длиннее шести букв 2538 (91%) есть ТОЛЬКО в стенограмме.
-- Фамилия «Елиупова», названная в разговоре один раз, полнотекстом по
-- content_text не находилась (0 строк), по content_extra находилась сразу
-- (события 483722). Векторы не помогали: brain-triage строит их из
-- llm_excerpt(content_text), и куски (event_chunk_embeddings) режутся оттуда
-- же — дословного текста не было ни в одном индексе.
--
-- Почему колонка, а не индекс по выражению из content_extra:
--   * ADD COLUMN без DEFAULT в pg11+ меняет только каталог — events (706 МБ,
--     445 тыс. строк) не переписывается, блокировка мгновенная;
--   * to_tsvector('indonesian', content_extra) индексировал бы и значения
--     служебных ключей («mic», «system», «voice_transcript») — запрос со
--     словом «system» матчил бы каждое голосовое событие;
--   * колонка заполняется на приёме (gateway/voice.py) и содержит только речь.
--
-- Почему не векторы по стенограмме: отказ был лексический (имя собственное,
-- сказанное один раз), а не смысловой. Куски всех 1.74 МБ стенограмм дали бы
-- ~1340 строк в event_chunk_embeddings (~3.6 МБ) и столько же вызовов
-- эмбеддинга; GIN по тем же 1.74 МБ стоит единицы мегабайт и ноль вызовов.
--
-- Цена индексов: transcript_text заполняется только у голосовых событий
-- (150 за всю историю, ~130/мес), у остальных NULL. to_tsvector(cfg, NULL)
-- = NULL, а NULL в GIN не попадает — оба индекса покрывают сотни строк.
--
-- CONCURRENTLY — без BEGIN/COMMIT (в транзакции запрещён). Упавшее построение
-- оставляет INVALID-индекс, который IF NOT EXISTS при повторе пропустит;
-- apply_migration.sh это ловит и в учёт не пишет. Тогда:
--   DROP INDEX CONCURRENTLY IF EXISTS ix_events_fts_transcript_russian;  и заново.
-- Откат: DROP обоих индексов + ALTER TABLE events DROP COLUMN transcript_text.
-- Код без индексов работает (условие уйдёт в seq scan), поэтому катить ДО
-- деплоя кода. Старые события остаются с NULL: обратное заполнение —
-- отдельная задача.

ALTER TABLE events ADD COLUMN IF NOT EXISTS transcript_text TEXT;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_fts_transcript_russian
    ON events USING gin (to_tsvector('russian', transcript_text));

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_fts_transcript_indonesian
    ON events USING gin (to_tsvector('indonesian', transcript_text));

INSERT INTO schema_migrations (version, note)
VALUES ('033_events_transcript_fts',
        'events.transcript_text + GIN: дословная речь стала находимой')
ON CONFLICT (version) DO NOTHING;

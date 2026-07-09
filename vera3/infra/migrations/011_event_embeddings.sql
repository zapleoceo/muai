-- Migration 011: вынос эмбеддингов из events в узкую таблицу event_embeddings.
--
-- Зачем: вектор ~6.5КБ в каждой строке раздул events до 3.9ГБ (эмбеддинги —
-- 2.5ГБ из них). Из-за этого любой COUNT(*)/GROUP BY для дашборда читал всю
-- таблицу (сканы 3-14с). После выноса events ужмётся до ~1.4ГБ, сканы ускорятся
-- в ~3x, а поиск джойнит вектор только когда он реально нужен.
--
-- Здесь — только создание таблицы (мгновенно). Бэкфилл 2.5ГБ делается батчами
-- скриптом (см. deploy-шаги), чтобы не держать одну огромную транзакцию.
-- DROP COLUMN events.embedding_voyage_3 + VACUUM FULL — отдельной миграцией
-- 012 ПОСЛЕ проверки, что поиск/дедуп читают из новой таблицы.

BEGIN;

CREATE TABLE IF NOT EXISTS event_embeddings (
    event_id   BIGINT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    embedding  JSONB NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

COMMIT;

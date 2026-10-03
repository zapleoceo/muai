-- Migration 039: снять JSONB-колонку event_embeddings.embedding, embedding_vec
-- сделать NOT NULL.
--
-- Накатывать ТОЛЬКО после 038 и после релиза кода, который JSONB не трогает
-- (любая реплика старого кода после DROP COLUMN упадёт на SELECT/INSERT).
-- Порядок и расчёт времени — docs/deploy-ops.md, «Снятие JSONB-эмбеддингов».
--
-- Файл без обёртывающей транзакции СОЗНАТЕЛЬНО (apply_migration.sh шлёт его в
-- psql командами по одной): VALIDATE CONSTRAINT сканирует ~1.2 ГБ heap, и на
-- время скана ему нужна только SHARE UPDATE EXCLUSIVE — запись не блокируется.
-- Короткие ACCESS EXCLUSIVE (ADD CONSTRAINT, SET NOT NULL, DROP COLUMN) идут
-- под lock_timeout. SET NOT NULL без этого трюка просканировал бы таблицу под
-- ACCESS EXCLUSIVE; с уже ВАЛИДНЫМ CHECK (embedding_vec IS NOT NULL) PG12+
-- пропускает скан.
--
-- DROP COLUMN место НЕ возвращает: старые значения лежат в TOAST (~3.5 ГБ)
-- до перезаписи таблицы — это отдельный шаг (VACUUM FULL), см. runbook.
--
-- Идемпотентна: повторный накат после обрыва продолжает с того же места.

\set ON_ERROR_STOP on
SET lock_timeout = '5s';

-- Предохранитель: тысячи строк без вектора значат, что реэмбеддинг не
-- закончен или что-то пишет в таблицу по-старому. Лучше остановиться.
DO $$
DECLARE n bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'event_embeddings' AND column_name = 'embedding_vec') THEN
        SELECT count(*) INTO n FROM event_embeddings WHERE embedding_vec IS NULL;
        IF n > 10000 THEN
            RAISE EXCEPTION 'строк без embedding_vec: % (> 10000) — сначала разобраться', n;
        END IF;
    ELSE
        RAISE EXCEPTION 'нет колонки embedding_vec — 030 не накатана';
    END IF;
END $$;

-- 1. С этого момента новая строка без вектора невозможна.
ALTER TABLE event_embeddings DROP CONSTRAINT IF EXISTS event_embeddings_vec_not_null;
ALTER TABLE event_embeddings
    ADD CONSTRAINT event_embeddings_vec_not_null CHECK (embedding_vec IS NOT NULL) NOT VALID;

-- 2. Строки без вектора (на 2026-10-03 — 2484, у всех JSON null: эмбеддинг
-- так и не получили) удаляются; события остаются, а цикл reembed в
-- brain-triage подберёт их как события без строки в event_embeddings.
DELETE FROM event_embeddings WHERE embedding_vec IS NULL;

-- 3. Проверка существующих строк — без блокировки записи.
SET lock_timeout = 0;
ALTER TABLE event_embeddings VALIDATE CONSTRAINT event_embeddings_vec_not_null;
SET lock_timeout = '5s';

-- 4. Проверенный CHECK позволяет SET NOT NULL без скана; сам CHECK больше не нужен.
ALTER TABLE event_embeddings ALTER COLUMN embedding_vec SET NOT NULL;
ALTER TABLE event_embeddings DROP CONSTRAINT event_embeddings_vec_not_null;

-- 5. Только каталог. Место вернёт VACUUM FULL (runbook).
ALTER TABLE event_embeddings DROP COLUMN IF EXISTS embedding;

INSERT INTO schema_migrations (version, note)
VALUES ('039_drop_event_embeddings_jsonb',
        'DROP COLUMN event_embeddings.embedding; embedding_vec NOT NULL; место — VACUUM FULL отдельно')
ON CONFLICT (version) DO NOTHING;

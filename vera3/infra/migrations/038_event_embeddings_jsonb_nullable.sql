-- Migration 038: event_embeddings.embedding (JSONB, наследие 011) — nullable.
--
-- Первый из двух шагов снятия JSONB-эмбеддингов (второй — 039). Код после
-- этого релиза пишет вектор ТОЛЬКО в embedding_vec (vectors.embedding_upsert),
-- и INSERT без JSONB упал бы на NOT NULL. Поэтому порядок такой:
--   1. накатить 038 (мгновенно — только каталог, данные не трогаются);
--   2. выкатить код, который JSONB не читает и не пишет;
--   3. не раньше чем через сутки спокойной работы — 039 (DROP COLUMN).
-- Старый код при этом продолжает работать: он пишет JSONB, а колонка теперь
-- просто разрешает NULL. Откат — SET NOT NULL, но только пока нет строк с
-- NULL в embedding; обычно откат не нужен.

BEGIN;

-- ALTER берёт ACCESS EXCLUSIVE: не даём повиснуть за долгой транзакцией и
-- заблокировать за собой весь поиск и триаж (упало по таймауту — повторить).
SET LOCAL lock_timeout = '5s';

ALTER TABLE event_embeddings ALTER COLUMN embedding DROP NOT NULL;

INSERT INTO schema_migrations (version, note)
VALUES ('038_event_embeddings_jsonb_nullable',
        'event_embeddings.embedding DROP NOT NULL; код пишет только embedding_vec')
ON CONFLICT (version) DO NOTHING;

COMMIT;

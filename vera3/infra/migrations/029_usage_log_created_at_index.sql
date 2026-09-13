-- Migration 029: индекс по usage_log.created_at + ретенция.
--
-- usage_log растёт на строку с КАЖДОГО LLM-вызова (broker_client._log_usage) и
-- ничем не чистится: политики хранения в репозитории не было вообще. При
-- 10-14 тыс. триажей в час это сотни тысяч строк в сутки.
--
-- Дашборд при этом считал по ней агрегат БЕЗ WHERE — только FILTER-ами, то
-- есть полным сканом всей накопленной истории, и делал это при каждом
-- обновлении кэша (TTL 60 c, плюс поллинг /_progress раз в 30 c, пока
-- страница открыта). Запрос теперь ограничен окном в 30 дней (самый широкий
-- FILTER там и есть :month, так что цифры не изменились), и под это окно
-- нужен индекс.
--
-- Существующие индексы для него не годятся: ix_usage_provider_date ведёт с
-- provider, ix_usage_event — с event_id. Диапазон по одному created_at ни
-- тем, ни другим не берётся.
--
-- Чистка старых строк — отдельно, scripts/prune_usage_log.sql по крону:
-- удаление внутри миграции сделало бы её неидемпотентной по времени и
-- заблокировало бы таблицу на неопределённый срок.

-- CONCURRENTLY и без BEGIN/COMMIT (13.09.2026, до наката): в usage_log
-- 1.15 млн строк / 336 МБ, и обычный CREATE INDEX держал бы SHARE-блокировку,
-- то есть стопорил бы запись КАЖДОГО LLM-вызова на всё время построения.
-- CONCURRENTLY внутри транзакции запрещён, поэтому транзакции нет; psql без
-- -1 (apply_migration.sh) шлёт команды по одной.
--
-- Ловушка IF NOT EXISTS: упавшее посреди построения CONCURRENTLY оставляет
-- индекс INVALID, и повторный накат его молча пропустит. Проверка после:
--   SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_usage_created_at'::regclass;
-- f → DROP INDEX CONCURRENTLY ix_usage_created_at и накатить заново.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_usage_created_at ON usage_log (created_at);

INSERT INTO schema_migrations (version, note)
VALUES ('029_usage_log_created_at_index', 'ix_usage_created_at + окно в агрегате дашборда')
ON CONFLICT (version) DO NOTHING;

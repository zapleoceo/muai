-- Ретенция usage_log. Ставится в root-крон рядом с vera-backup.sh:
--
--   15 4 * * *  docker exec -i vera3-postgres psql -qU vera -d vera \
--                 < /var/www/vera3/scripts/prune_usage_log.sql
--
-- Зачем: таблица растёт на строку с каждого LLM-вызова и раньше не чистилась
-- ничем. Сырые строки нужны для разбора инцидента и сверки с биллингом
-- брокера — то есть на недели, не на годы. Дашборд дальше 30 дней в неё и не
-- смотрит (окно в dashboard/stats.py), так что 90 дней — троекратный запас.
--
-- Порциями по 50 тыс. с COMMIT в цикле: одно большое DELETE держало бы
-- блокировку на всю чистку и раздуло WAL. Скрипт идемпотентен — можно гонять
-- хоть каждый час, на пустом хвосте он выходит с первой итерации.
--
-- COMMIT внутри DO требует PostgreSQL 11+ (у нас pg16) и autocommit —
-- поэтому НЕ оборачивать вызов в BEGIN/END снаружи.
--
-- Срок правится здесь, одним числом: тащить его в app_control незачем,
-- крон-скрипт всё равно правится руками на сервере.

DO $$
DECLARE
    cutoff timestamp := now() - interval '90 days';
    killed integer;
    total  integer := 0;
BEGIN
    LOOP
        DELETE FROM usage_log
        WHERE id IN (
            SELECT id FROM usage_log WHERE created_at < cutoff LIMIT 50000
        );
        GET DIAGNOSTICS killed = ROW_COUNT;
        total := total + killed;
        EXIT WHEN killed = 0;
        COMMIT;
    END LOOP;
    RAISE NOTICE 'usage_log: удалено % строк старше %', total, cutoff;
END $$;

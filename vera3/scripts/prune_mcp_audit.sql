-- Ретенция mcp_audit (журнал правок агентов через /mcp). НЕ в кроне по умолчанию:
-- запускает оператор, когда журнал разрастётся (каждая запись хранит снимки
-- before/after, то есть прежний текст события целиком).
--
--   docker exec -i vera3-postgres psql -qU vera -d vera < /var/www/vera3/scripts/prune_mcp_audit.sql
--
-- Цена чистки: запись старше срока больше нельзя откатить через `undo` —
-- прежняя версия текста уходит вместе с ней. Поэтому срок большой (180 дней),
-- а откат записи-отката (`undo_of`) чистится вместе с исходной.
-- Порциями с COMMIT в цикле, как scripts/prune_usage_log.sql; autocommit, без BEGIN снаружи.

DO $$
DECLARE
    cutoff timestamp := now() - interval '180 days';
    killed integer;
    total  integer := 0;
BEGIN
    LOOP
        DELETE FROM mcp_audit
        WHERE id IN (
            SELECT id FROM mcp_audit WHERE created_at < cutoff LIMIT 10000
        );
        GET DIAGNOSTICS killed = ROW_COUNT;
        total := total + killed;
        EXIT WHEN killed = 0;
        COMMIT;
    END LOOP;
    RAISE NOTICE 'mcp_audit: удалено % строк старше %', total, cutoff;
END $$;

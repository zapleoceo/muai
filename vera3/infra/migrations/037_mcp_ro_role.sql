-- 037: роль vera_ro — подключение для MCP-инструмента sql_query.
--
-- Основная роль `vera` — суперпользователь, и разбор текста запроса её не
-- удерживает: `SELECT query_to_xml('select pg_read_file(''/etc/passwd'')', …)`
-- прячет опасный SQL в строковый литерал. sql_query поэтому ходит ролью без
-- суперправ: SELECT только на таблицы с содержимым мозга, секретных таблиц
-- (gmail_accounts, telegram_sessions, instagram_sessions, slack_auth,
-- trello_boards, app_control, очереди с текстом поручений) в списке нет.
--
-- Пароль миграция НЕ задаёт (его не должно быть в git). Оператор:
--   ALTER ROLE vera_ro PASSWORD '<openssl rand -hex 24>';
-- и кладёт в infra/.env:
--   MCP_RO_DATABASE_URL=postgresql+asyncpg://vera_ro:<пароль>@postgres:5432/vera
-- Без этой переменной sql_query отказывается работать (clear error).
--
-- Идемпотентна: повторный накат пересобирает права с нуля.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vera_ro') THEN
        CREATE ROLE vera_ro LOGIN;
    END IF;
END
$$;

ALTER ROLE vera_ro NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN;
ALTER ROLE vera_ro SET default_transaction_read_only = on;
ALTER ROLE vera_ro SET statement_timeout = '10s';
ALTER ROLE vera_ro SET idle_in_transaction_session_timeout = '30s';

DO $$
DECLARE
    t text;
    content_tables text[] := ARRAY[
        'events', 'event_embeddings', 'event_chunk_embeddings',
        'entities', 'entity_aliases', 'memberships', 'relationships',
        'merge_suggestions', 'project_membership', 'patterns', 'identity_nodes',
        'usage_log', 'mcp_audit'
    ];
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO vera_ro', current_database());
    GRANT USAGE ON SCHEMA public TO vera_ro;
    REVOKE CREATE ON SCHEMA public FROM vera_ro;
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM vera_ro;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM vera_ro;
    FOREACH t IN ARRAY content_tables LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON public.%I TO vera_ro', t);
        END IF;
    END LOOP;
END
$$;

INSERT INTO schema_migrations (version, note)
VALUES ('037_mcp_ro_role',
        'роль vera_ro (без суперправ, SELECT на таблицы содержимого) для sql_query')
ON CONFLICT (version) DO NOTHING;

COMMIT;

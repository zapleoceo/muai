-- 052: персистентный журнал запросов /mcp — только метаданные (cid, метод, актор, исход).
--
-- Тел, params/arguments, заголовков и токенов здесь нет и быть не должно.
-- Ретенция 30 дней — в scripts/prune_usage_log.sql. Таблица новая, старый код не затронут.
--
-- Откат: удалить таблицу mcp_request_log (данных, нужных кому-либо ещё, в ней нет).

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS mcp_request_log (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMP NOT NULL DEFAULT NOW(),
    cid VARCHAR(64) NOT NULL,
    method VARCHAR(8),
    actor VARCHAR(64),
    ua VARCHAR(80),
    rpc VARCHAR(64),
    tool VARCHAR(64),
    rpc_id VARCHAR(32),
    status SMALLINT,
    ms INTEGER,
    outcome VARCHAR(24)
);

CREATE INDEX IF NOT EXISTS ix_mcp_request_log_at ON mcp_request_log (at);
CREATE INDEX IF NOT EXISTS ix_mcp_request_log_cid ON mcp_request_log (cid);

INSERT INTO schema_migrations (version, note)
VALUES ('052_mcp_request_log', 'журнал /mcp: персистентные метаданные запросов, ретенция 30 дней')
ON CONFLICT (version) DO NOTHING;

COMMIT;

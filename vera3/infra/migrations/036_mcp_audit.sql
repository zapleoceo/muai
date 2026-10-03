-- 036: mcp_audit — журнал записей агентов через удалённый MCP (/mcp).
--
-- Каждая запись агента в мозг (remember, update_event, hide_event, правки
-- графа) кладёт сюда строку: кто (client — имя токена), что (tool, args),
-- над чем (target_kind/target_id) и состояние ДО и ПОСЛЕ. По before/after
-- инструмент `undo` откатывает правку. Ничего не удаляется: скрытие события
-- — статус triage_status='hidden', связь снимается is_current=false.
--
-- Таблица только дописывается; status меняется один раз (applied → undone).

BEGIN;

CREATE TABLE IF NOT EXISTS mcp_audit (
    id          BIGSERIAL PRIMARY KEY,
    client      VARCHAR(64) NOT NULL,
    tool        VARCHAR(64) NOT NULL,
    args        JSONB NOT NULL DEFAULT '{}'::jsonb,
    target_kind VARCHAR(32) NOT NULL,
    target_id   BIGINT,
    before      JSONB,
    after       JSONB,
    status      VARCHAR(16) NOT NULL DEFAULT 'applied',
    undo_of     BIGINT,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_mcp_audit_created_at ON mcp_audit (created_at);
CREATE INDEX IF NOT EXISTS ix_mcp_audit_target ON mcp_audit (target_kind, target_id);

INSERT INTO schema_migrations (version, note)
VALUES ('036_mcp_audit',
        'журнал записей агентов через удалённый MCP: before/after для undo')
ON CONFLICT (version) DO NOTHING;

COMMIT;

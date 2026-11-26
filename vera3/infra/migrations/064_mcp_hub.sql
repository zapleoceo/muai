-- MCP-хаб (docs/mcp-hub.md): апстримы по проектам, агенты с правами, журнал вызовов.
-- Создаёт три таблицы и стартовые записи апстримов БЕЗ секретов. Идемпотентна.
-- Применять ДО выкладки сервиса mcp-hub и админки /hub.

BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS hub_upstreams (
    id              BIGSERIAL    PRIMARY KEY,
    project         VARCHAR(16)  NOT NULL,
    slug            VARCHAR(16)  NOT NULL,
    title           VARCHAR(128) NOT NULL DEFAULT '',
    transport       VARCHAR(8)   NOT NULL DEFAULT 'http',
    url             TEXT         NOT NULL DEFAULT '',
    command         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    auth_kind       VARCHAR(8)   NOT NULL DEFAULT 'none',
    auth_header     VARCHAR(64)  NOT NULL DEFAULT '',
    secret_enc      TEXT,
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    hint            TEXT         NOT NULL DEFAULT '',
    timeout_s       INTEGER      NOT NULL DEFAULT 30,
    max_concurrency INTEGER      NOT NULL DEFAULT 4,
    health          VARCHAR(16)  NOT NULL DEFAULT 'unknown',
    health_error    VARCHAR(200) NOT NULL DEFAULT '',
    health_at       TIMESTAMP,
    tool_count      INTEGER      NOT NULL DEFAULT 0,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hub_upstreams_project_slug UNIQUE (project, slug),
    CONSTRAINT ck_hub_upstreams_secret_sealed CHECK (secret_enc IS NULL OR secret_enc LIKE 'enc1:%')
);

CREATE TABLE IF NOT EXISTS hub_agents (
    id           BIGSERIAL   PRIMARY KEY,
    name         VARCHAR(32) NOT NULL UNIQUE,
    token_hash   VARCHAR(64) NOT NULL UNIQUE,
    token_prefix VARCHAR(12) NOT NULL DEFAULT '',
    allow        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
    note         TEXT        NOT NULL DEFAULT '',
    created_at   TIMESTAMP   NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hub_calls (
    id          BIGSERIAL    PRIMARY KEY,
    agent       VARCHAR(32)  NOT NULL,
    method      VARCHAR(32)  NOT NULL,
    tool        VARCHAR(160) NOT NULL DEFAULT '',
    project     VARCHAR(16)  NOT NULL DEFAULT '',
    upstream    VARCHAR(16)  NOT NULL DEFAULT '',
    status      VARCHAR(16)  NOT NULL,
    duration_ms INTEGER      NOT NULL DEFAULT 0,
    error       VARCHAR(120) NOT NULL DEFAULT '',
    created_at  TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hub_calls_created_at ON hub_calls (created_at);

INSERT INTO hub_upstreams (project, slug, title, transport, url, auth_kind, enabled, hint) VALUES
 ('vera', 'room', 'Vera room — комната агентов', 'http', 'http://mcp:8000/mcp', 'bearer', TRUE,
  'Сервисный токен hub из ROOM_TOKENS, заводится на сервере (python -m vera_hub.cli secret).'),
 ('vera', 'memory', 'Vera — личная память', 'http', 'http://mcp:8000/mcp', 'bearer', TRUE,
  'Сервисный токен hub из MCP_TOKENS. Полный доступ к памяти: выдавать агентам только по решению Димы.'),
 ('aib', 'video', 'Broker video MCP', 'http', 'https://aib.zapleo.com/mcp/video', 'bearer', TRUE,
  'Ключ приватного video-only проекта Broker (aib_prj_…). Генерация платная.'),
 ('sniffer', 'catalog', 'SnifferBot — публичный каталог', 'http', 'https://lbot.zapleo.com/mcp/catalog', 'none', TRUE,
  'Публичный, без ключа.'),
 ('sniffer', 'personal', 'SnifferBot — личные рассылки', 'http', 'https://lbot.zapleo.com/mcp/personal', 'bearer', TRUE,
  'Личный ключ Димы (как по /mcp в боте), выпускается на сервере Sniffer.'),
 ('itstep', 'crm', 'IT STEP CRM', 'http', 'https://mcp.itstep.org/mcp/crm', 'url', TRUE,
  'Ждёт секрета: токен в URL выдаёт IT STEP, заносит Дима (секрет = полный URL с токеном). Агенты не вводят.'),
 ('itstep', 'sintos', 'SintOS (IT STEP)', 'http', 'https://itstep.sinhrm.itsrv.xyz/api/v1/itstep/mcp', 'bearer', TRUE,
  'Ждёт секрета: bearer-токен выдаёт IT STEP, заносит Дима. Агенты не вводят.'),
 ('itstep', 'atlassian', 'Atlassian (Jira / Confluence)', 'http', '', 'bearer', FALSE,
  'Ждёт решения Димы: нужен свой API-токен или OAuth-клиент Atlassian; коннектор claude.ai в хаб не переносится.'),
 ('personal', 'google', 'Google (Gmail / Drive / Calendar)', 'http', '', 'bearer', FALSE,
  'Ждёт решения Димы: нужен собственный OAuth-клиент Google на сервере.'),
 ('personal', 'telegram', 'Telegram — личный аккаунт', 'stdio', '', 'env', FALSE,
  'Ждёт решения Димы: перенос личной MTProto-сессии на сервер.'),
 ('stepan', 'stepan', 'Stepan2', 'stdio', '', 'none', FALSE,
  'stdio-сервер stepan_mcp.py живёт на ноутбуке; для хаба его нужно поднять HTTP-сервисом в стеке stepan2.')
ON CONFLICT (project, slug) DO NOTHING;

INSERT INTO schema_migrations (version, note)
VALUES ('064_mcp_hub', 'MCP-хаб: апстримы по проектам, агенты хаба, журнал вызовов')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- Rollback: остановить mcp-hub, затем DROP TABLE hub_calls, hub_agents, hub_upstreams (только с подтверждения владельца).

-- 047: agent room — комната, где агенты (Claude, Codex, …) переписываются и
-- делят задачи через MCP, без доступа к личной памяти.
--
-- Отдельный набор токенов ROOM_TOKENS ведёт на отдельный MCP-сервер, в котором
-- есть только room_*-инструменты (см. vera_mcp/room_tools.py). Отправитель
-- (from_agent, lease_holder, agent) — имя токена, а не то, что агент написал о
-- себе: подделать автора нельзя.
--
-- room_messages — только дописывается. message_id задаёт клиент: повтор того же
--   message_id в той же комнате не создаёт второе сообщение (идемпотентность).
-- room_tasks — владение задачей через аренду (lease_until) и fencing_token:
--   каждый новый захват увеличивает токен, и правка с устаревшим токеном
--   отвергается, даже если старый владелец «проснулся» после истечения аренды.
-- room_cursors — до какого сообщения агент дочитал свой входящий поток.

BEGIN;

CREATE TABLE IF NOT EXISTS room_messages (
    id          BIGSERIAL PRIMARY KEY,
    room        VARCHAR(64)  NOT NULL,
    message_id  VARCHAR(128) NOT NULL,
    from_agent  VARCHAR(64)  NOT NULL,
    to_agent    VARCHAR(64),
    task_id     VARCHAR(128),
    in_reply_to VARCHAR(128),
    status      VARCHAR(16)  NOT NULL DEFAULT 'info',
    body        TEXT         NOT NULL,
    created_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_room_messages_message_id UNIQUE (room, message_id)
);

CREATE INDEX IF NOT EXISTS ix_room_messages_room_id ON room_messages (room, id);

CREATE TABLE IF NOT EXISTS room_tasks (
    room          VARCHAR(64)  NOT NULL,
    task_id       VARCHAR(128) NOT NULL,
    title         TEXT,
    created_by    VARCHAR(64)  NOT NULL,
    status        VARCHAR(16)  NOT NULL DEFAULT 'open',
    lease_holder  VARCHAR(64),
    lease_until   TIMESTAMP,
    fencing_token BIGINT       NOT NULL DEFAULT 0,
    paths         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    note          TEXT,
    created_at    TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (room, task_id)
);

CREATE TABLE IF NOT EXISTS room_cursors (
    agent           VARCHAR(64) NOT NULL,
    room            VARCHAR(64) NOT NULL,
    last_message_id BIGINT      NOT NULL DEFAULT 0,
    updated_at      TIMESTAMP   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent, room)
);

INSERT INTO schema_migrations (version, note)
VALUES ('047_agent_room', 'комната агентов: сообщения, задачи с арендой и fencing, курсоры')
ON CONFLICT (version) DO NOTHING;

COMMIT;

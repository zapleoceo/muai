-- 053: MCP Events — подписки на события задач комнаты и транзакционный outbox.
--
-- Секрет подписки (whsec_…) лежит только зашифрованным (Fernet, ключ ROOM_OAUTH_KEY,
-- как клиенты в 048); тела доставок хранятся как отправляются, секретов в них нет.
-- Новые таблицы, старый код не затронут. Откат: ничего не удаляем; остановить доставку
-- можно UPDATE room_event_subscriptions SET status = 'revoked'.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS room_event_subscriptions (
    id varchar(64) PRIMARY KEY,
    principal varchar(64) NOT NULL,
    room varchar(64) NOT NULL,
    event_name varchar(16) NOT NULL,
    task_id varchar(128),
    kinds jsonb NOT NULL DEFAULT '[]'::jsonb,
    callback_url text NOT NULL,
    encrypted_secret text NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at timestamp NOT NULL DEFAULT NOW(),
    expires_at timestamp
);

CREATE INDEX IF NOT EXISTS ix_room_event_subs_match
    ON room_event_subscriptions (room, event_name, status);

CREATE TABLE IF NOT EXISTS room_event_outbox (
    id bigserial PRIMARY KEY,
    subscription_id varchar(64) NOT NULL
        REFERENCES room_event_subscriptions (id) ON DELETE CASCADE,
    event_id varchar(64) NOT NULL,
    event_name varchar(16) NOT NULL,
    task_id varchar(128) NOT NULL,
    body text NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'dropped', 'revoked', 'failed')),
    attempts smallint NOT NULL DEFAULT 0,
    next_attempt_at timestamp NOT NULL DEFAULT NOW(),
    last_error varchar(200),
    created_at timestamp NOT NULL DEFAULT NOW(),
    delivered_at timestamp,
    source_event_id bigint NOT NULL,
    CONSTRAINT uq_room_event_outbox_event UNIQUE (subscription_id, event_id)
);

CREATE INDEX IF NOT EXISTS ix_room_event_outbox_due
    ON room_event_outbox (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_room_event_outbox_task
    ON room_event_outbox (subscription_id, task_id, id);

INSERT INTO schema_migrations (version, note)
VALUES ('053_mcp_events', 'MCP Events: подписки и transactional outbox')
ON CONFLICT (version) DO NOTHING;

COMMIT;

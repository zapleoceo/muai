-- Пульт зависших задач владельца в Telegram: одна строка состояния одного сообщения.
-- Только создаёт таблицу; данные не меняются. Применять ДО выкладки кода бота со стражем.

BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS stall_board (
    name            VARCHAR(64)  PRIMARY KEY,
    message_id      BIGINT,
    posted_at       TIMESTAMP,
    rev             BIGINT       NOT NULL DEFAULT 0,
    current_room    VARCHAR(64),
    current_task_id VARCHAR(128),
    mode            VARCHAR(16)  NOT NULL DEFAULT 'ask',
    skipped         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    rendered        TEXT,
    updated_at      TIMESTAMP
);

INSERT INTO schema_migrations (version, note)
VALUES ('063_stall_board', 'Пульт зависших задач владельца: одно сообщение, правится на месте')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- Rollback: выключить STALL_BOARD_ENABLED и выкатить прежний код. Таблицу можно оставить.

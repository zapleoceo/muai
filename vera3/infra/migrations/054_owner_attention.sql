-- 054: внимание владельца (active|wait10|quiet) и отложенные уведомления.
-- Новые таблицы, старый код не затронут; без флага OWNER_QUIET_ENABLED=1 политика не действует.
-- Откат: ничего не удаляем; политику выключает флаг OWNER_QUIET_ENABLED=0.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS owner_attention (
    id smallint PRIMARY KEY CHECK (id = 1),
    state varchar(8) NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'wait10', 'quiet')),
    wait_question_ref varchar(128),
    wait_deadline timestamp,
    quiet_until timestamp,
    updated_at timestamp NOT NULL DEFAULT NOW()
);

INSERT INTO owner_attention (id, state) VALUES (1, 'active') ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS owner_deferred (
    id bigserial PRIMARY KEY,
    dedup_key varchar(160) NOT NULL UNIQUE,
    kind varchar(16) NOT NULL,
    room varchar(64),
    task_id varchar(128),
    question_id integer,
    text text NOT NULL,
    created_at timestamp NOT NULL DEFAULT NOW(),
    sent_at timestamp
);

CREATE INDEX IF NOT EXISTS ix_owner_deferred_pending ON owner_deferred (sent_at, id);

-- Доставка вопросов владельцу в Telegram: уже существующие вопросы считаем доставленными,
-- иначе после включения флага на владельца свалится вся история.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'room_task_questions' AND column_name = 'delivered_at') THEN
        ALTER TABLE room_task_questions ADD COLUMN delivered_at timestamp;
        UPDATE room_task_questions SET delivered_at = NOW();
    END IF;
END $$;

COMMIT;

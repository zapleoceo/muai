-- 049: трекер задач комнаты, шаг 1 — журнал событий и поля задачи.
--
-- Только добавляет: колонки room_tasks с безопасными значениями по умолчанию и
-- новые таблицы. Старый код продолжает работать без изменений.
--
-- room_task_events — append-only журнал: что и когда происходило с задачей.
--   heartbeat (продление аренды) и progress (содержательный прогресс) —
--   разные события: last_progress_at двигает только progress.
-- room_task_questions / room_task_answers / watchdog_state — для следующих
--   шагов трекера; создаются сейчас, чтобы не плодить миграции.

BEGIN;

ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 2;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS owner VARCHAR(64);
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS next_action TEXT;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS refs JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS holder_session VARCHAR(128);
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS holder_account VARCHAR(64);
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMP;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS last_progress_text TEXT;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS next_checkpoint_at TIMESTAMP;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS pending_handoff_to VARCHAR(64);
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS plan_start TIMESTAMP;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS plan_end TIMESTAMP;

CREATE TABLE IF NOT EXISTS room_task_events (
    id            BIGSERIAL PRIMARY KEY,
    room          VARCHAR(64)  NOT NULL,
    task_id       VARCHAR(128) NOT NULL,
    at            TIMESTAMP    NOT NULL DEFAULT NOW(),
    kind          VARCHAR(32)  NOT NULL,
    agent         VARCHAR(64),
    session       VARCHAR(128),
    account       VARCHAR(64),
    fencing_token BIGINT,
    text          TEXT,
    data          JSONB,
    CONSTRAINT ck_room_task_events_kind CHECK (kind IN ('created', 'claimed', 'progress', 'heartbeat', 'paused', 'resumed', 'review', 'blocked', 'unblocked', 'question', 'answered', 'ack_answer', 'handoff_offer', 'handoff_accept', 'released', 'done', 'lease_expired', 'watchdog_action'))
);

CREATE INDEX IF NOT EXISTS ix_room_task_events_task ON room_task_events (room, task_id, id);
CREATE INDEX IF NOT EXISTS ix_room_task_events_kind_at ON room_task_events (kind, at);

CREATE TABLE IF NOT EXISTS room_task_questions (
    qid       BIGSERIAL PRIMARY KEY,
    room      VARCHAR(64)  NOT NULL,
    task_id   VARCHAR(128) NOT NULL,
    asked_by  VARCHAR(64)  NOT NULL,
    question  TEXT         NOT NULL,
    asked_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
    status    VARCHAR(16)  NOT NULL DEFAULT 'open',
    acked_at  TIMESTAMP,
    ack_by    VARCHAR(64),
    CONSTRAINT ck_room_task_questions_status
        CHECK (status IN ('open', 'answered', 'acked', 'withdrawn'))
);

CREATE INDEX IF NOT EXISTS ix_room_task_questions_open
    ON room_task_questions (room, task_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS room_task_answers (
    id          BIGSERIAL PRIMARY KEY,
    qid         BIGINT      NOT NULL REFERENCES room_task_questions (qid),
    text        TEXT        NOT NULL,
    answered_by VARCHAR(64) NOT NULL,
    answered_at TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS watchdog_state (
    name        VARCHAR(64) PRIMARY KEY,
    last_run_at TIMESTAMP,
    last_error  TEXT
);

INSERT INTO schema_migrations (version, note)
VALUES ('049_room_task_tracker', 'трекер задач комнаты: журнал событий, поля задачи, вопросы, watchdog')
ON CONFLICT (version) DO NOTHING;

COMMIT;

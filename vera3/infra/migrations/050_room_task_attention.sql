-- 050: трекер задач комнаты, шаг 2b — внимание: проект, зависимости, автоподбор, ожидание.
--
-- Колонки только добавляются (константные DEFAULT в PG 11+ не переписывают таблицу).
-- CHECK-ограничения — через NOT VALID + VALIDATE: добавление мгновенное, проверка
-- старых строк идёт под слабой блокировкой, не мешая чтению и записи.
-- Старый код не страдает: priority и так пишется только в 0..3, next_action ≤ 2000.
--
-- Откат: DROP COLUMN для пяти колонок, DROP CONSTRAINT для двух новых CHECK и
-- возврат ck_room_task_events_kind к списку из 049.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS project VARCHAR(64);
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS depends_on JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS auto_pickup BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS waiting_until TIMESTAMP;
ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS waiting_reason TEXT;

ALTER TABLE room_tasks DROP CONSTRAINT IF EXISTS ck_room_tasks_priority;
ALTER TABLE room_tasks ADD CONSTRAINT ck_room_tasks_priority
    CHECK (priority BETWEEN 0 AND 3) NOT VALID;
ALTER TABLE room_tasks VALIDATE CONSTRAINT ck_room_tasks_priority;

ALTER TABLE room_tasks DROP CONSTRAINT IF EXISTS ck_room_tasks_next_action_len;
ALTER TABLE room_tasks ADD CONSTRAINT ck_room_tasks_next_action_len
    CHECK (char_length(next_action) <= 2000) NOT VALID;
ALTER TABLE room_tasks VALIDATE CONSTRAINT ck_room_tasks_next_action_len;

-- вид события 'waiting' (законное ожидание внешнего события)
ALTER TABLE room_task_events DROP CONSTRAINT IF EXISTS ck_room_task_events_kind;
ALTER TABLE room_task_events ADD CONSTRAINT ck_room_task_events_kind
    CHECK (kind IN ('created', 'claimed', 'progress', 'heartbeat', 'paused', 'resumed', 'review', 'blocked', 'unblocked', 'question', 'answered', 'ack_answer', 'handoff_offer', 'handoff_accept', 'released', 'done', 'lease_expired', 'watchdog_action', 'waiting')) NOT VALID;
ALTER TABLE room_task_events VALIDATE CONSTRAINT ck_room_task_events_kind;

INSERT INTO schema_migrations (version, note)
VALUES ('050_room_task_attention', 'трекер задач, шаг 2b: project, depends_on, auto_pickup, waiting_*, CHECK priority/next_action, вид события waiting')
ON CONFLICT (version) DO NOTHING;

COMMIT;

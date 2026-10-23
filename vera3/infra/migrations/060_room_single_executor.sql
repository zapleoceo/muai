-- 060 (room B1m): схема для единственного исполнителя диспетчера комнаты.
--   * room_tasks.recovery_hold — «после recover никто не берёт задачу до решения о старте».
--     BOOLEAN NOT NULL DEFAULT false: константный DEFAULT в PG >= 11 — только метаданные,
--     без перезаписи таблицы.
--   * room_singletons(name, owner_run, epoch, expires_at) — строка лидерства (один живой
--     экземпляр диспетчера). Новая пустая таблица; пишет её только код B1+.
--   * CHECK ck_room_task_events_kind расширяется видами recovered, start_approved,
--     wait_resolved, rebind, run_unverified, question_closed; все прежние 22 вида сохранены.
-- Данные не удаляются и не меняются. Идемпотентна (повторный накат безопасен).
-- Применять ДО выкладки кода B1, который пишет эти значения; сам по себе старый код
-- новые колонку/таблицу игнорирует. Деплой миграции не катит: scripts/apply_migration.sh.

BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS recovery_hold BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS room_singletons (
    name       VARCHAR(64) PRIMARY KEY,
    owner_run  VARCHAR(64) NOT NULL,
    epoch      BIGINT      NOT NULL DEFAULT 0,
    expires_at TIMESTAMP   NOT NULL
);

ALTER TABLE room_task_events DROP CONSTRAINT IF EXISTS ck_room_task_events_kind;
ALTER TABLE room_task_events ADD CONSTRAINT ck_room_task_events_kind
    CHECK (kind IN ('created', 'claimed', 'progress', 'heartbeat', 'paused', 'resumed',
                   'review', 'blocked', 'unblocked', 'question', 'answered', 'ack_answer',
                   'handoff_offer', 'handoff_accept', 'released', 'done', 'lease_expired',
                   'watchdog_action', 'waiting', 'delegated_attestation',
                   'metadata_updated', 'cancelled',
                   'recovered', 'start_approved', 'wait_resolved', 'rebind',
                   'run_unverified', 'question_closed')) NOT VALID;

COMMIT;

BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE room_task_events VALIDATE CONSTRAINT ck_room_task_events_kind;
INSERT INTO schema_migrations (version, note)
VALUES ('060_room_single_executor', 'room_tasks.recovery_hold, room_singletons, 6 new event kinds')
ON CONFLICT (version) DO NOTHING;
COMMIT;

-- Rollback: ничего не удаляется. Откат = redeploy предыдущего кода; расширенный CHECK,
-- колонка recovery_hold, таблица room_singletons и все события остаются на месте
-- (старый код их не использует). Физическое удаление (DROP COLUMN/TABLE) — только
-- отдельным решением владельца, в рамках этой миграции не предусмотрено.

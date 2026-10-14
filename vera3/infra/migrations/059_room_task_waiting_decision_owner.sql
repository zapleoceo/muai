-- Explicit "who must decide" label for a waiting task (room_task_wait decision_owner).
-- Nullable column, no default: no table rewrite, old code ignores it. Idempotent.
-- Apply BEFORE deploying code that writes it, via scripts/apply_migration.sh.

BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE room_tasks ADD COLUMN IF NOT EXISTS waiting_decision_owner VARCHAR(64);

INSERT INTO schema_migrations (version, note)
VALUES ('059_room_task_waiting_decision_owner', 'room_task_wait decision_owner column')
ON CONFLICT (version) DO NOTHING;
COMMIT;

-- Rollback: redeploy the previous code, then
--   ALTER TABLE room_tasks DROP COLUMN IF EXISTS waiting_decision_owner;

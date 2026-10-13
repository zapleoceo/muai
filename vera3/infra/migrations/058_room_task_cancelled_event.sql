-- Add an audit kind for the standard room_task_cancel. Do not delete audit events.
-- Apply BEFORE deploying code that exposes room_task_cancel; no data is changed.

BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE room_task_events DROP CONSTRAINT IF EXISTS ck_room_task_events_kind;
ALTER TABLE room_task_events ADD CONSTRAINT ck_room_task_events_kind
    CHECK (kind IN ('created', 'claimed', 'progress', 'heartbeat', 'paused', 'resumed',
                   'review', 'blocked', 'unblocked', 'question', 'answered', 'ack_answer',
                   'handoff_offer', 'handoff_accept', 'released', 'done', 'lease_expired',
                   'watchdog_action', 'waiting', 'delegated_attestation',
                   'metadata_updated', 'cancelled')) NOT VALID;

COMMIT;

BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE room_task_events VALIDATE CONSTRAINT ck_room_task_events_kind;
INSERT INTO schema_migrations (version, note)
VALUES ('058_room_task_cancelled_event', 'Audit kind for room_task_cancel')
ON CONFLICT (version) DO NOTHING;
COMMIT;

-- Rollback: redeploy the previous code. Keep the expanded CHECK and all
-- cancelled audit events intact.

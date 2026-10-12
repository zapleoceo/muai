-- Add an audit kind for metadata-only task corrections. Do not delete audit events.
-- Apply only after a separate approval for this migration and the security flag.

BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE room_task_events DROP CONSTRAINT IF EXISTS ck_room_task_events_kind;
ALTER TABLE room_task_events ADD CONSTRAINT ck_room_task_events_kind
    CHECK (kind IN ('created', 'claimed', 'progress', 'heartbeat', 'paused', 'resumed',
                   'review', 'blocked', 'unblocked', 'question', 'answered', 'ack_answer',
                   'handoff_offer', 'handoff_accept', 'released', 'done', 'lease_expired',
                   'watchdog_action', 'waiting', 'delegated_attestation',
                   'metadata_updated')) NOT VALID;

COMMIT;

BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE room_task_events VALIDATE CONSTRAINT ck_room_task_events_kind;
INSERT INTO schema_migrations (version, note)
VALUES ('057_room_task_metadata_event', 'Audit kind for lease-free task metadata corrections')
ON CONFLICT (version) DO NOTHING;
COMMIT;

-- Rollback: set ROOM_METADATA_ENABLED=0 and redeploy the previous code. Keep
-- the expanded CHECK and all metadata_updated audit events intact.

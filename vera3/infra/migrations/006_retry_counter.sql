-- Migration 006: triage retry counter + next-retry-at
--
-- Today 2018 events stuck in triage_status='error' have no automatic
-- recovery. After the underlying bug is fixed (e.g. record_free_usage)
-- they never get re-tried.
--
-- This adds:
--   - triage_retry_count  — how many times we've re-pended
--   - triage_next_retry_at — earliest moment retry-worker should pick it up
--   - status 'dead' for events that exhausted retries
--
-- Watchdog logic (in brain-triage worker):
--   * SELECT events WHERE status='error' AND retry_count < 5
--     AND (next_retry_at IS NULL OR next_retry_at < NOW())
--   * Set status='pending', retry_count += 1, next_retry_at = NOW() + backoff
--     backoff: 1min, 5min, 30min, 2h, 12h
--   * If retry_count = 5 → mark 'dead' (drops out of retry)

BEGIN;

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS triage_retry_count INT NOT NULL DEFAULT 0;

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS triage_next_retry_at TIMESTAMP NULL;

-- Existing errors: schedule their first retry in 1 minute so they catch up
-- after this migration deploys. retry_count stays 0 so they get full 5 tries.
UPDATE events
SET triage_next_retry_at = NOW() + INTERVAL '1 minute'
WHERE triage_status = 'error'
  AND triage_next_retry_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_events_retry_due
  ON events (triage_next_retry_at)
  WHERE triage_status = 'error' AND triage_retry_count < 5;

COMMIT;

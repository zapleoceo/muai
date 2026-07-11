-- Migration 014: partial index for brain-triage's claim query
--
-- _claim_batch() (brain_triage/worker.py) polls every TRIAGE_POLL_INTERVAL_S
-- (5s) × BRAIN_TRIAGE_REPLICAS (5) with:
--   SELECT id FROM events
--   WHERE triage_status = 'pending' AND content_text <> ''
--   ORDER BY occurred_at DESC LIMIT :batch FOR UPDATE SKIP LOCKED
--
-- events already has partial indexes for triage_status='processing'
-- (ix_events_processing_started, migration 002) and 'error'
-- (ix_events_retry_due, migration 006) — 'pending' was the missing case.
-- Without it, once the backlog empties (0 pending rows), the planner has
-- no way to skip non-pending rows: it walks ix_events_occurred_at
-- backwards filtering row-by-row, i.e. a near-full scan of the whole
-- table on every single poll. Measured on production 2026-07-11 with
-- ~403k rows: 2.9s per call, 387k buffer touches, 0 rows found — ×5
-- replicas ×every 5s ≈ sustained ~100% CPU on a 2-core box (load avg 3.4+
-- on 2 cores) doing nothing but confirming there's no work to do.
--
-- After this index: same query, ~0.05ms, 1 buffer touch.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_events_pending_claim
  ON events (occurred_at DESC)
  WHERE triage_status = 'pending' AND content_text <> '';

COMMIT;

-- NOTE: applied CONCURRENTLY on production ahead of this file (CONCURRENTLY
-- can't run inside a transaction block, and this repo doesn't have a
-- migration runner — see docs/deploy-ops.md). This file is the durable
-- record for fresh deploys / disaster recovery; on a fresh/small database
-- the plain (transactional) CREATE INDEX above is fine.

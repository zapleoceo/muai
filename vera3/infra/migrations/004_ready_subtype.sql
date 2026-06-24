-- Migration 004: ready_subtype classification
-- Separates "ready" leads into sales vs. event flows.
--
-- Subtypes:
--   null        — not ready (default)
--   'deal'      — ready to BUY (has contact, purchase intent, within cohort)
--   'openhouse' — ready to ATTEND (June 29 Open House event)
--
-- Backward compatibility: existing ready events default to 'deal' (primary sales flow).
--
-- Apply via:
--   docker exec vera3-postgres psql -U vera -d vera -f /tmp/004.sql
-- Or interactively:
--   psql -U vera -d vera
--   \i /tmp/004.sql

BEGIN;

-- ─ Add column ──────────────────────────────────────────────────────────
ALTER TABLE events
  ADD COLUMN IF NOT EXISTS ready_subtype VARCHAR(20) DEFAULT NULL;

-- ─ Backward compatibility: seed existing ready events ──────────────────
-- All events marked needs_action=true (ready for action) → 'deal' subtype
-- This is the primary sales flow; new 'openhouse' events will be classified by LLM.
UPDATE events
SET ready_subtype = 'deal'
WHERE (triage_metadata->>'needs_action')::boolean = true
  AND ready_subtype IS NULL;

-- ─ Index for fast dashboard queries ────────────────────────────────────
CREATE INDEX IF NOT EXISTS ix_events_ready_subtype
  ON events (ready_subtype)
  WHERE (triage_metadata->>'needs_action')::boolean = true;

-- ─ Document column ────────────────────────────────────────────────────
COMMENT ON COLUMN events.ready_subtype IS
  'Lead ready status subtype: null (not ready) | deal (ready to buy) | openhouse (ready to attend event). Updated by brain-triage classifier.';

COMMIT;

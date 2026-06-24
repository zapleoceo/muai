-- Ready Subtypes — SQL Reference & Monitoring Queries
-- Use these to monitor, troubleshoot, and analyze ready leads

-- ════════════════════════════════════════════════════════════════════════════════
-- 1. VERIFICATION — Check migration applied correctly
-- ════════════════════════════════════════════════════════════════════════════════

-- Verify column exists and is correct type
SELECT
  column_name,
  data_type,
  is_nullable
FROM information_schema.columns
WHERE table_name = 'events'
  AND column_name = 'ready_subtype';

-- Expected output:
--  column_name   | data_type | is_nullable
-- ───────────────┼───────────┼────────────
--  ready_subtype | character varying(20) | YES


-- Verify index was created
SELECT
  schemaname,
  tablename,
  indexname,
  indexdef
FROM pg_indexes
WHERE tablename = 'events'
  AND indexname = 'ix_events_ready_subtype';


-- ════════════════════════════════════════════════════════════════════════════════
-- 2. DASHBOARD QUERIES — What managers see
-- ════════════════════════════════════════════════════════════════════════════════

-- All ready leads (any subtype)
SELECT
  id,
  source,
  account,
  content_text,
  ready_subtype,
  occurred_at,
  triage_metadata->>'people_mentioned' as people,
  triage_metadata->>'topics' as topics
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
ORDER BY occurred_at DESC
LIMIT 50;


-- Ready leads: Deal flow only (ready to BUY)
SELECT
  id,
  source,
  account,
  content_text,
  occurred_at
FROM events
WHERE ready_subtype = 'deal'
ORDER BY occurred_at DESC;


-- Ready leads: OpenHouse flow only (ready to ATTEND June 29)
SELECT
  id,
  source,
  account,
  content_text,
  occurred_at
FROM events
WHERE ready_subtype = 'openhouse'
ORDER BY occurred_at DESC;


-- Ready leads with metadata details
SELECT
  id,
  source,
  ready_subtype,
  occurred_at,
  triage_metadata->>'people_mentioned' as person,
  triage_metadata->>'topics' as topics,
  triage_metadata->>'ready_signal_type' as signal_type,
  content_text
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
ORDER BY
  CASE
    WHEN ready_subtype = 'deal' THEN 0
    WHEN ready_subtype = 'openhouse' THEN 1
    ELSE 2
  END,
  occurred_at DESC;


-- ════════════════════════════════════════════════════════════════════════════════
-- 3. STATISTICS — Count, trend, distribution
-- ════════════════════════════════════════════════════════════════════════════════

-- Summary: count by subtype
SELECT
  ready_subtype,
  COUNT(*) as count,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) as pct
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
GROUP BY ready_subtype
ORDER BY count DESC;

-- Example output:
--  ready_subtype | count | pct
-- ───────────────┼───────┼──────
--  deal          |    12 | 80.0
--  openhouse     |     3 | 20.0
--  (NULL)        |     0 |  0.0


-- Trend: ready leads by day, by subtype
SELECT
  DATE(occurred_at) as day,
  ready_subtype,
  COUNT(*) as count
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
  AND occurred_at >= NOW() - INTERVAL '30 days'
GROUP BY DATE(occurred_at), ready_subtype
ORDER BY day DESC, ready_subtype;


-- Leads by source + subtype (where did they come from?)
SELECT
  source,
  ready_subtype,
  COUNT(*) as count
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
GROUP BY source, ready_subtype
ORDER BY count DESC;

-- Example: see which channels produce deal vs openhouse leads


-- Leads by project + subtype (which project do they relate to?)
SELECT
  project,
  ready_subtype,
  COUNT(*) as count
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
GROUP BY project, ready_subtype
ORDER BY count DESC;


-- ════════════════════════════════════════════════════════════════════════════════
-- 4. DATA QUALITY — Spot issues
-- ════════════════════════════════════════════════════════════════════════════════

-- Events marked needs_action=true but ready_subtype is NULL
-- (should only happen if LLM couldn't infer, or legacy data before migration)
SELECT
  id,
  source,
  account,
  content_text,
  triage_metadata->>'importance' as importance,
  occurred_at
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
  AND ready_subtype IS NULL
ORDER BY occurred_at DESC;

-- Action: review these and potentially re-triage or manually classify


-- Invalid ready_subtype values (should not exist, but check)
SELECT
  id,
  ready_subtype,
  content_text
FROM events
WHERE ready_subtype NOT IN ('deal', 'openhouse')
  AND ready_subtype IS NOT NULL;

-- If any rows, it means LLM or app sent invalid value — debug


-- Events with ready_subtype but needs_action=false (inconsistency)
SELECT
  id,
  ready_subtype,
  triage_metadata->>'needs_action' as needs_action,
  content_text
FROM events
WHERE ready_subtype IS NOT NULL
  AND (triage_metadata->>'needs_action')::boolean IS NOT true;

-- Action: fix by setting ready_subtype = NULL for these


-- ════════════════════════════════════════════════════════════════════════════════
-- 5. DETAILED LEAD VIEW — Manager drill-down
-- ════════════════════════════════════════════════════════════════════════════════

-- Single lead with all details (replace :id with actual event ID)
SELECT
  id,
  source,
  source_event_id,
  account,
  category,
  content_text,
  ready_subtype,
  (triage_metadata->>'people_mentioned')::text as people_mentioned,
  (triage_metadata->>'topics')::text as topics,
  (triage_metadata->>'signals')::text as signals,
  occurred_at,
  received_at,
  nature,
  project,
  importance
FROM events
WHERE id = :id;


-- All messages from a person/account (to track history)
SELECT
  id,
  source,
  occurred_at,
  ready_subtype,
  (triage_metadata->>'needs_action')::text as action_needed,
  content_text
FROM events
WHERE account = :account
ORDER BY occurred_at DESC;


-- ════════════════════════════════════════════════════════════════════════════════
-- 6. BULK OPERATIONS — Admin use only
-- ════════════════════════════════════════════════════════════════════════════════

-- DANGEROUS: Manually re-classify ready leads (use only if LLM failed)
--
-- Example: Mark all leads from Instagram mentioning "course" as deal:
UPDATE events
SET ready_subtype = 'deal'
WHERE source = 'instagram'
  AND (triage_metadata->>'needs_action')::boolean = true
  AND ready_subtype IS NULL
  AND content_text ILIKE '%course%';

-- Verify before running:
SELECT COUNT(*) FROM events WHERE source = 'instagram' AND ready_subtype IS NULL;


-- Re-classify all NULL-subtype ready leads as 'deal' (fallback):
UPDATE events
SET ready_subtype = 'deal'
WHERE (triage_metadata->>'needs_action')::boolean = true
  AND ready_subtype IS NULL;


-- Reset a single lead (in case of misclassification):
UPDATE events
SET ready_subtype = 'openhouse'
WHERE id = :id;


-- ════════════════════════════════════════════════════════════════════════════════
-- 7. ADVANCED ANALYTICS — For weekly reports
-- ════════════════════════════════════════════════════════════════════════════════

-- Conversion funnel (if you track conversion status separately):
-- This is conceptual — implement based on your conversion tracking table
WITH ready_leads AS (
  SELECT
    id,
    ready_subtype,
    occurred_at
  FROM events
  WHERE (triage_metadata->>'needs_action')::boolean = true
    AND occurred_at >= NOW() - INTERVAL '7 days'
)
SELECT
  ready_subtype,
  COUNT(*) as ready_count,
  0 as converted_count,  -- would query conversions table here
  'pending' as status
FROM ready_leads
GROUP BY ready_subtype;


-- Time-to-action: how long from event to first action?
-- (useful if you add action_timestamps in triage_metadata)
SELECT
  ready_subtype,
  AVG(EXTRACT(EPOCH FROM (created_at - occurred_at)) / 3600)::int as avg_hours_to_triage,
  MAX(EXTRACT(EPOCH FROM (created_at - occurred_at)) / 3600)::int as max_hours
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
GROUP BY ready_subtype;


-- Importance distribution: are deal leads more urgent than openhouse?
SELECT
  ready_subtype,
  AVG((triage_metadata->>'importance')::int) as avg_importance,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (triage_metadata->>'importance')::int) as median_importance,
  MIN((triage_metadata->>'importance')::int) as min_importance,
  MAX((triage_metadata->>'importance')::int) as max_importance
FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
GROUP BY ready_subtype;


-- ════════════════════════════════════════════════════════════════════════════════
-- 8. TESTING & DEBUGGING — Development queries
-- ════════════════════════════════════════════════════════════════════════════════

-- Find events that SHOULD be classified as deal (for testing LLM prompt)
SELECT
  id,
  content_text,
  ready_subtype,
  triage_metadata->>'people_mentioned' as has_contact
FROM events
WHERE content_text ILIKE '%хочу купить%'
  OR content_text ILIKE '%ready to buy%'
  OR content_text ILIKE '%счёт%'
  OR content_text ILIKE '%payment%'
LIMIT 10;


-- Find events that SHOULD be classified as openhouse
SELECT
  id,
  content_text,
  ready_subtype
FROM events
WHERE content_text ILIKE '%open house%'
  OR content_text ILIKE '%29 июня%'
  OR content_text ILIKE '%29 june%'
  OR content_text ILIKE '%мероприятие%'
LIMIT 10;


-- Quick test: re-triage a single event (if you implement retry endpoint)
SELECT
  id,
  triage_status,
  triage_error
FROM events
WHERE id = :id;

-- If triage_status='error', you can:
-- 1. Update to 'pending'
-- 2. Clear triage_started_at
-- 3. Next worker batch will re-process
UPDATE events
SET triage_status = 'pending', triage_started_at = NULL
WHERE id = :id;


-- ════════════════════════════════════════════════════════════════════════════════
-- 9. EXPORT — For external tools (CRM, spreadsheet, etc.)
-- ════════════════════════════════════════════════════════════════════════════════

-- Export ready leads as CSV (use with psql \copy or DBeaver export)
-- For deal flow leads:
COPY (
  SELECT
    id,
    'deal' as type,
    source,
    account,
    content_text,
    triage_metadata->>'people_mentioned' as contact,
    triage_metadata->>'topics' as topics,
    occurred_at
  FROM events
  WHERE ready_subtype = 'deal'
  ORDER BY occurred_at DESC
)
TO STDOUT WITH CSV HEADER;

-- Usage in shell:
-- psql -U vera -d vera -c "COPY (...) TO STDOUT" > deals.csv


-- ════════════════════════════════════════════════════════════════════════════════
-- 10. MAINTENANCE — Keep data clean
-- ════════════════════════════════════════════════════════════════════════════════

-- Archive old ready leads (older than 60 days, already processed)
-- Move to events_archive table if you have one
DELETE FROM events
WHERE ready_subtype IS NOT NULL
  AND occurred_at < NOW() - INTERVAL '60 days';

-- Check how many would be deleted first:
SELECT COUNT(*) FROM events
WHERE ready_subtype IS NOT NULL
  AND occurred_at < NOW() - INTERVAL '60 days';


-- Refresh index (after many updates/deletes):
REINDEX INDEX ix_events_ready_subtype;

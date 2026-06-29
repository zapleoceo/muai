-- Migration 009: drop backfill_jobs
--
-- The one-shot Telegram history backfill (→ 2025-06-01) finished 2026-06-29:
-- 6067 dialogs walked, ~323k messages inserted into events. The queue table
-- was operational state for that run — not content. All messages live in
-- `events`; the journal of which dialog reached which cursor has no ongoing
-- value.
--
-- Retired alongside: ingestor-telegram backfill_worker.py + the
-- scripts/seed_backfill_queue.py seeder + the dashboard /backfill page.
-- Live ingestion (userbot on_new) covers everything going forward.
--
-- Safe: dropping this table loses no event data. If a deeper/again backfill
-- is ever needed, re-apply migration 007 and restore the worker from git
-- history.

DROP TABLE IF EXISTS backfill_jobs;

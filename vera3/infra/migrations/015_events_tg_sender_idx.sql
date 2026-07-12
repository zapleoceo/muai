-- Migration 015: index events by Telegram sender for per-entity dossiers.
--
-- The dedup UI and entity dossier look up a person's messages via
-- metadata->>'sender_id'. Without an index that's a full seq scan of ~400k
-- events per candidate — the /entities/duplicates page and get_entity_context's
-- recent-count both crawled. Partial index (telegram only) keeps it small.
--
-- CONCURRENTLY → no write lock on the hot events table; must run OUTSIDE a
-- transaction (no BEGIN/COMMIT here).

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_tg_sender
  ON events ((metadata->>'sender_id'))
  WHERE source = 'telegram';

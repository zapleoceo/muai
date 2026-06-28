-- Migration 007: backfill_jobs queue
--
-- Replaces the one-shot scripts/auth_tg_userbot backfill flow with a
-- persistent queue: one row per (dialog, target_floor_date). The backfill
-- worker iterates the queue, walking each dialog backwards in pages,
-- updating cursor_msg_id as it goes. Survives flood-wait, container
-- restarts, and partial completion.
--
-- States:
--   pending      — created, not yet picked up
--   in_progress  — worker is actively walking back in this dialog
--   completed    — reached target_floor_date or beginning of history
--   error        — terminal failure (no access, deleted dialog, etc.)
--
-- One worker, one in_progress at a time (Telethon session is single-tenant).

BEGIN;

CREATE TABLE IF NOT EXISTS backfill_jobs (
    id                  SERIAL PRIMARY KEY,
    chat_id             BIGINT NOT NULL,
    chat_title          TEXT,
    target_floor_date   TIMESTAMP NOT NULL,
    -- Telethon offset_id — start from this msg_id going backwards.
    -- NULL = start from newest. 1 = reached start. Updated after every page.
    cursor_msg_id       BIGINT,
    cursor_oldest_date  TIMESTAMP,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    last_error          TEXT,
    pages_done          INT NOT NULL DEFAULT 0,
    messages_inserted   INT NOT NULL DEFAULT 0,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMP,
    finished_at         TIMESTAMP,
    UNIQUE (chat_id, target_floor_date)
);

CREATE INDEX IF NOT EXISTS ix_backfill_due
  ON backfill_jobs (status, created_at)
  WHERE status IN ('pending', 'in_progress');

COMMIT;

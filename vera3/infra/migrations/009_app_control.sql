-- 009: app_control — runtime key/value flags polled by workers.
-- First use: backfill_paused (dashboard Pause/Resume button). 2026-06-29.

CREATE TABLE IF NOT EXISTS app_control (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

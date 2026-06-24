-- Migration 002: add events.triage_started_at + index
-- Применяется через: docker exec vera3-postgres psql -U vera -d vera -f /tmp/002.sql
--
-- Решает баг с watchdog'ом: использовал received_at (когда событие пришло),
-- а должен использовать triage_started_at (когда воркер захватил его).

BEGIN;

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS triage_started_at TIMESTAMP WITHOUT TIME ZONE;

CREATE INDEX IF NOT EXISTS ix_events_processing_started
  ON events (triage_started_at)
  WHERE triage_status = 'processing';

-- На случай если есть события застрявшие в processing с момента до миграции:
-- сразу возвращаем их в pending. Без этого watchdog не сможет их подобрать
-- (triage_started_at = NULL, condition `< NOW() - interval` не сработает).
UPDATE events
SET triage_status = 'pending'
WHERE triage_status = 'processing'
  AND triage_started_at IS NULL;

COMMIT;

-- Disposable integration-test reset only; never discard live capture history.
BEGIN;
DROP TABLE IF EXISTS brain_gmail_pilot_events;
DROP TABLE IF EXISTS brain_gmail_pilot_pages;
DROP TABLE IF EXISTS brain_gmail_pilot_resyncs;
DROP TABLE IF EXISTS brain_gmail_pilot_accounts;
DELETE FROM schema_migrations WHERE version='049_gmail_shadow_pilot';
COMMIT;

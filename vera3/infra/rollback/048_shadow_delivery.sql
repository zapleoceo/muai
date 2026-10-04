-- Disposable-test reset only; production rollback must preserve captured envelopes.
BEGIN;
DROP TABLE IF EXISTS brain_delivery_envelopes;
DROP TABLE IF EXISTS brain_delivery_batches;
DROP TABLE IF EXISTS brain_delivery_cursors;
DELETE FROM schema_migrations WHERE version = '048_shadow_delivery';
COMMIT;

-- Test-only rollback of the additive 047 schema; never part of automated deploy.
BEGIN;
DROP TABLE IF EXISTS brain_derived_cache;
DROP TABLE IF EXISTS brain_event_links;
DROP TABLE IF EXISTS brain_rollback_audit;
DROP TABLE IF EXISTS brain_claims;
DROP TABLE IF EXISTS brain_generations;
DROP TABLE IF EXISTS brain_revisions;
DROP TABLE IF EXISTS brain_checkpoints;
DROP TABLE IF EXISTS brain_source_objects;
DELETE FROM schema_migrations WHERE version = '047_shadow_brain';
COMMIT;

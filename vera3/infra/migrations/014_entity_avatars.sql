-- Migration 014: entity_avatars — profile photos for graph/dedup UI.
--
-- Blobs kept in a SIDE table (not entities) so the hot entities table stays
-- lean — same reasoning as 011 pulling embeddings out of events. One row per
-- entity, filled lazily+throttled by the ingestor-telegram avatar backfill.
-- `missing=true` marks "checked, no photo / privacy-hidden" so the backfill
-- doesn't re-hammer Telegram for accounts that have no avatar.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_avatars (
    entity_id  INTEGER PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    image      BYTEA,
    mime       TEXT NOT NULL DEFAULT 'image/jpeg',
    missing    BOOLEAN NOT NULL DEFAULT FALSE,
    fetched_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

COMMIT;

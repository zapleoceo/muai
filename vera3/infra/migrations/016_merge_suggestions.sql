-- Migration 016: merge_suggestions — Vera's cross-name identity analysis.
--
-- The LLM identity judge (vera_shared/graph/identity.py) compares candidate
-- entity pairs (Маша ↔ Matia Ivanova, Оля ↔ Ольга …) using their dossiers and
-- writes same-person suggestions here. The owner accepts (→ merge_entities)
-- or rejects on /entities/duplicates; rejected pairs are never re-asked.
-- (entity_a, entity_b) is stored ordered a<b so the pair is unique either way.

BEGIN;

CREATE TABLE IF NOT EXISTS merge_suggestions (
    id          SERIAL PRIMARY KEY,
    entity_a    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    verdict     TEXT NOT NULL,               -- same | unsure | different
    confidence  REAL NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|accepted|rejected
    created_at  TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_merge_pair UNIQUE (entity_a, entity_b)
);

COMMIT;

-- Checkpoint 1 substrate: L1 (entities/aliases/memberships/relationships),
-- L2 (patterns), L3 (identity_nodes).

CREATE TABLE IF NOT EXISTS entities (
  id SERIAL PRIMARY KEY,
  type VARCHAR(40) NOT NULL,
  name VARCHAR(500) NOT NULL,
  canonical_id VARCHAR(200),
  attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at TIMESTAMP NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_entities_type_name ON entities(type, name);

CREATE TABLE IF NOT EXISTS entity_aliases (
  id SERIAL PRIMARY KEY,
  entity_id INT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  source VARCHAR(40) NOT NULL,
  identifier VARCHAR(500) NOT NULL,
  display_name VARCHAR(500),
  confidence FLOAT NOT NULL DEFAULT 1.0,
  CONSTRAINT uq_alias_source_identifier UNIQUE (source, identifier)
);
CREATE INDEX IF NOT EXISTS ix_alias_entity ON entity_aliases(entity_id);

CREATE TABLE IF NOT EXISTS memberships (
  id SERIAL PRIMARY KEY,
  parent_entity_id INT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  child_entity_id INT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  source VARCHAR(40) NOT NULL,
  role VARCHAR(40),
  attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at TIMESTAMP NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMP NOT NULL DEFAULT now(),
  is_current BOOLEAN NOT NULL DEFAULT true,
  CONSTRAINT uq_membership UNIQUE (parent_entity_id, child_entity_id, source)
);
CREATE INDEX IF NOT EXISTS ix_membership_parent ON memberships(parent_entity_id);

CREATE TABLE IF NOT EXISTS relationships (
  id SERIAL PRIMARY KEY,
  subject_entity_id INT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  object_entity_id INT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  predicate VARCHAR(80) NOT NULL,
  fact TEXT,
  confidence FLOAT NOT NULL DEFAULT 0.6,
  derived_from_event_id INT,
  first_seen_at TIMESTAMP NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMP NOT NULL DEFAULT now(),
  is_current BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS ix_rel_subject ON relationships(subject_entity_id);
CREATE INDEX IF NOT EXISTS ix_rel_object ON relationships(object_entity_id);

CREATE TABLE IF NOT EXISTS patterns (
  id SERIAL PRIMARY KEY,
  trigger_signature VARCHAR(500) NOT NULL,
  action_kind VARCHAR(80) NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  observation_count INT NOT NULL DEFAULT 0,
  correction_count INT NOT NULL DEFAULT 0,
  weight FLOAT NOT NULL DEFAULT 0.0,
  first_seen_at TIMESTAMP NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_pattern UNIQUE (trigger_signature, action_kind)
);

CREATE TABLE IF NOT EXISTS identity_nodes (
  id SERIAL PRIMARY KEY,
  type VARCHAR(40) NOT NULL,
  label VARCHAR(500) NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  weight FLOAT NOT NULL DEFAULT 1.0,
  listener_entity_id INT REFERENCES entities(id) ON DELETE SET NULL,
  derived_from JSONB NOT NULL DEFAULT '{}'::jsonb,
  valid_from TIMESTAMP,
  valid_until TIMESTAMP,
  confidence FLOAT NOT NULL DEFAULT 0.7,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now(),
  is_current BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS ix_identity_type_listener ON identity_nodes(type, listener_entity_id);

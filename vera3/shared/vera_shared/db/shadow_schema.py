"""Offline shadow schema; production migration remains separately gated."""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
 provider TEXT NOT NULL, account_id TEXT NOT NULL, object_type TEXT NOT NULL,
 external_id TEXT NOT NULL, active_generation INTEGER, source_head_revision TEXT,
 PRIMARY KEY(provider, account_id, object_type, external_id));
CREATE TABLE IF NOT EXISTS revisions (
 provider TEXT NOT NULL, account_id TEXT NOT NULL, object_type TEXT NOT NULL,
 external_id TEXT NOT NULL, revision TEXT NOT NULL, content_hash TEXT NOT NULL,
 received_at TEXT NOT NULL, required_scope TEXT NOT NULL, deleted INTEGER NOT NULL,
 previous_revision TEXT,
 PRIMARY KEY(provider, account_id, object_type, external_id, revision));
CREATE TABLE IF NOT EXISTS generations (
 id INTEGER PRIMARY KEY, provider TEXT NOT NULL, account_id TEXT NOT NULL,
 object_type TEXT NOT NULL, external_id TEXT NOT NULL, revision TEXT NOT NULL,
 extraction_version TEXT NOT NULL, known_from TEXT NOT NULL, known_to TEXT,
 UNIQUE(provider, account_id, object_type, external_id, revision, extraction_version));
CREATE TABLE IF NOT EXISTS claims (
 generation_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, subject TEXT NOT NULL,
 predicate TEXT NOT NULL, value TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
 evidence_anchor TEXT NOT NULL, evidence_kind TEXT NOT NULL,
 PRIMARY KEY(generation_id, ordinal));
CREATE TABLE IF NOT EXISTS checkpoints (
 provider TEXT NOT NULL, account_id TEXT NOT NULL, cursor TEXT NOT NULL,
 PRIMARY KEY(provider, account_id));
CREATE TABLE IF NOT EXISTS rollback_audit (
 id INTEGER PRIMARY KEY, provider TEXT NOT NULL, account_id TEXT NOT NULL,
 object_type TEXT NOT NULL, external_id TEXT NOT NULL,
 from_generation INTEGER NOT NULL, target_generation INTEGER NOT NULL,
 restored_generation INTEGER NOT NULL, known_at TEXT NOT NULL, reason TEXT NOT NULL);
"""

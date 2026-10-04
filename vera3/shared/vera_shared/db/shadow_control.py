"""Audited, compare-and-swap restoration of an earlier shadow generation."""

from __future__ import annotations

import sqlite3

from vera_shared.ingest.shadow_types import Quarantine


def restore_generation(
    db: sqlite3.Connection,
    key: tuple[str, str, str, str],
    *,
    target_generation: int,
    expected_active: int,
    known_at: str,
    reason: str,
) -> int:
    if not reason.strip():
        raise Quarantine("rollback reason required")
    active = db.execute(
        "SELECT o.active_generation,g.known_from FROM objects o "
        "JOIN generations g ON g.id=o.active_generation "
        "WHERE o.provider=? AND o.account_id=? AND o.object_type=? AND o.external_id=?",
        key,
    ).fetchone()
    if active is None or active[0] != expected_active or target_generation == expected_active:
        raise Quarantine("rollback active generation changed")
    target = db.execute(
        "SELECT revision,extraction_version,known_from FROM generations "
        "WHERE id=? AND provider=? AND account_id=? AND object_type=? AND external_id=?",
        (target_generation, *key),
    ).fetchone()
    if target is None or known_at <= active[1] or known_at <= target[2]:
        raise Quarantine("invalid rollback target or knowledge time")
    db.execute("UPDATE generations SET known_to=? WHERE id=?", (known_at, expected_active))
    restored = db.execute(
        "INSERT INTO generations(provider,account_id,object_type,external_id,"
        "revision,extraction_version,known_from) VALUES (?,?,?,?,?,?,?)",
        (*key, target[0], f"restore:{target_generation}:{known_at}", known_at),
    ).lastrowid
    assert restored is not None
    db.execute(
        "INSERT INTO claims SELECT ?,ordinal,subject,predicate,value,valid_from,valid_to,"
        "evidence_anchor,evidence_kind FROM claims WHERE generation_id=?",
        (restored, target_generation),
    )
    db.execute(
        "UPDATE objects SET active_generation=? WHERE provider=? AND account_id=? "
        "AND object_type=? AND external_id=?",
        (restored, *key),
    )
    db.execute(
        "INSERT INTO rollback_audit(provider,account_id,object_type,external_id,"
        "from_generation,target_generation,restored_generation,known_at,reason) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (*key, expected_active, target_generation, restored, known_at, reason),
    )
    return restored

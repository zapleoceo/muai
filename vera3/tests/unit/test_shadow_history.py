"""Historical reads and controlled undo use synthetic sources only."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest

from shadow_samples import decision, make_brain, source
from vera_shared.ingest.shadow_types import Quarantine

KEY = source().key
ACCESS = {"telegram:owner-a"}
WRITE_ACCESS = {"write:telegram:owner-a"}


@pytest.mark.asyncio
async def test_as_of_tombstone_and_current_revocation(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    await brain.apply(source(), [decision()], known_at="2026-10-03T01:00:00Z")
    await brain.apply(
        replace(source(revision="2"), deleted=True), [], known_at="2026-10-03T02:00:00Z"
    )
    assert len(await brain.claims_as_of(ACCESS, "2026-10-03T01:30:00Z")) == 1
    assert await brain.claims_as_of(ACCESS, "2026-10-03T02:30:00Z") == []
    assert await brain.current_claims(ACCESS) == []
    assert await brain.claims_as_of(set(), "2026-10-03T01:30:00Z") == []
    assert await brain.claims_as_of({"telegram:owner-b"}, "2026-10-03T01:30:00Z") == []
    with pytest.raises(Quarantine, match="source access denied"):
        await brain.generation_ids(KEY, set())


@pytest.mark.asyncio
async def test_rollback_restores_claim_with_audit_and_preserves_as_of(tmp_path):
    path = tmp_path / "shadow.db"
    brain = await make_brain(path)
    await brain.apply(source(), [decision()], known_at="2026-10-03T01:00:00Z")
    await brain.apply(
        replace(source(revision="2"), deleted=True), [], known_at="2026-10-03T02:00:00Z"
    )
    first, tombstone = await brain.generation_ids(KEY, ACCESS)
    restored = await brain.rollback(
        KEY,
        target_generation=first,
        expected_active=tombstone,
        known_at="2026-10-03T03:00:00Z",
        reason="verified source correction",
        authorized_write_scopes=WRITE_ACCESS,
    )
    assert restored != first
    assert [row["value"] for row in await brain.current_claims(ACCESS)] == [
        "Ship on Monday"
    ]
    assert await brain.claims_as_of(ACCESS, "2026-10-03T02:30:00Z") == []
    assert len(await brain.claims_as_of(ACCESS, "2026-10-03T03:30:00Z")) == 1
    assert await brain.claims_as_of(set(), "2026-10-03T03:30:00Z") == []
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT from_generation,target_generation,restored_generation,reason "
            "FROM rollback_audit"
        ).fetchall() == [(tombstone, first, restored, "verified source correction")]


@pytest.mark.asyncio
async def test_rollback_rejects_wrong_actor_stale_active_and_blank_reason(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    await brain.apply(source(), [decision()], known_at="2026-10-03T01:00:00Z")
    await brain.apply(
        source(revision="2"), [decision("Changed")], known_at="2026-10-03T02:00:00Z"
    )
    first, second = await brain.generation_ids(KEY, ACCESS)
    params = dict(
        target_generation=first,
        expected_active=second,
        known_at="2026-10-03T03:00:00Z",
        reason="correction",
    )
    with pytest.raises(Quarantine, match="source write access denied"):
        await brain.rollback(KEY, authorized_write_scopes=ACCESS, **params)
    with pytest.raises(Quarantine, match="reason required"):
        await brain.rollback(
            KEY, authorized_write_scopes=WRITE_ACCESS, **(params | {"reason": " "})
        )
    await brain.rollback(KEY, authorized_write_scopes=WRITE_ACCESS, **params)
    with pytest.raises(Quarantine, match="active generation changed"):
        await brain.rollback(
            KEY,
            authorized_write_scopes=WRITE_ACCESS,
            **(params | {"known_at": "2026-10-03T04:00:00Z"}),
        )


@pytest.mark.asyncio
async def test_concurrent_checkpoint_compare_and_swap(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    one = source()
    two = replace(source(), external_id="chat-7:43", content_hash="another")
    results = await asyncio.gather(
        brain.apply(one, [decision()], known_at="2026-10-03T01:00:00Z", cursor="42"),
        brain.apply(two, [decision()], known_at="2026-10-03T01:00:00Z", cursor="43"),
        return_exceptions=True,
    )
    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, Quarantine) for result in results) == 1
    assert await brain.checkpoint("telegram", "owner-a") in {"42", "43"}
    assert len(await brain.current_claims(ACCESS)) == 1


@pytest.mark.asyncio
async def test_rollback_does_not_rewind_upstream_revision_head(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    await brain.apply(source(), [decision()], known_at="2026-10-03T01:00:00Z")
    await brain.apply(
        replace(source(revision="2"), deleted=True), [], known_at="2026-10-03T02:00:00Z"
    )
    first, second = await brain.generation_ids(KEY, ACCESS)
    await brain.rollback(
        KEY,
        target_generation=first,
        expected_active=second,
        known_at="2026-10-03T03:00:00Z",
        reason="restore display",
        authorized_write_scopes=WRITE_ACCESS,
    )
    assert await brain.apply(
        source(revision="3"),
        [decision("New source edit")],
        known_at="2026-10-03T04:00:00Z",
    )
    assert [row["value"] for row in await brain.current_claims(ACCESS)] == [
        "New source edit"
    ]

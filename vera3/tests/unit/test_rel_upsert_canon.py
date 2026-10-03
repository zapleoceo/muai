"""upsert_relationship пишет каноническую форму и разрешает противоречия."""
from __future__ import annotations

import pytest
from sqlalchemy import text
from vera_shared.graph import repo


async def _two(prefix=""):
    a = await repo.upsert_entity(type="person", name=f"{prefix}A B", source="s",
                                 identifier=f"{prefix}a")
    b = await repo.upsert_entity(type="person", name=f"{prefix}C D", source="s",
                                 identifier=f"{prefix}b")
    return a, b


async def _rows(get_session):
    async with get_session() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT subject_entity_id, predicate, object_entity_id, is_current "
            "FROM relationships ORDER BY id"))).all()]


@pytest.mark.asyncio
async def test_symmetric_stored_once_in_min_max_order(sqlite_db):
    a, b = await _two()
    lo, hi = sorted((a, b))
    assert await repo.upsert_relationship(subject_entity_id=hi, object_entity_id=lo,
                                          predicate="coworker_of", derived_from_event_id=1)
    assert not await repo.upsert_relationship(subject_entity_id=lo, object_entity_id=hi,
                                              predicate="coworker_of", derived_from_event_id=2)
    assert await _rows(sqlite_db) == [(lo, "coworker_of", hi, True)]


@pytest.mark.asyncio
async def test_reports_to_stored_as_boss_of_and_legacy_row_found(sqlite_db):
    a, b = await _two()
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="reports_to", derived_from_event_id=1)
    assert await _rows(sqlite_db) == [(b, "boss_of", a, True)]
    async with sqlite_db() as s:
        await s.execute(text("UPDATE relationships SET subject_entity_id=:a, "
                             "predicate='reports_to', object_entity_id=:b"), {"a": a, "b": b})
    assert not await repo.upsert_relationship(subject_entity_id=b, object_entity_id=a,
                                              predicate="boss_of", derived_from_event_id=2)
    assert len(await _rows(sqlite_db)) == 1


@pytest.mark.asyncio
async def test_weaker_contradiction_is_not_written(sqlite_db):
    a, b = await _two()
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="boss_of", confidence=0.9, derived_from_event_id=1)
    assert not await repo.upsert_relationship(
        subject_entity_id=b, object_entity_id=a, predicate="boss_of", confidence=0.7,
        derived_from_event_id=2)
    assert await _rows(sqlite_db) == [(a, "boss_of", b, True)]


@pytest.mark.asyncio
async def test_stronger_contradiction_retires_existing(sqlite_db):
    a, b = await _two()
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="boss_of", confidence=0.6, derived_from_event_id=1)
    assert await repo.upsert_relationship(
        subject_entity_id=b, object_entity_id=a, predicate="boss_of", confidence=0.95,
        derived_from_event_id=2)
    assert await _rows(sqlite_db) == [(a, "boss_of", b, False), (b, "boss_of", a, True)]


@pytest.mark.asyncio
async def test_resolve_strong_identifier_email_and_username(sqlite_db):
    eid = await repo.upsert_entity(type="person", name="X Y", source="gmail",
                                   identifier="x@y.com", attributes={"username": "Xy_1"})
    from vera_shared.graph.repo_relationships import resolve_strong_identifier
    assert await resolve_strong_identifier("X@Y.com") == eid
    assert await resolve_strong_identifier("@xy_1") == eid
    assert await resolve_strong_identifier("Андрей") is None
    assert await resolve_strong_identifier("@nobody") is None

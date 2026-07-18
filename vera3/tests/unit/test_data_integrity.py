"""Fix 5 — целостность данных: merge_entities без самопетель/дублей связей
(реальная SQLite + unique-индекс как в миграции 017), media-worker guard от
двойного append, remember() пишет эмбеддинг сразу (слепое окно дедупа)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from vera_shared.db import models, models_graph  # noqa: F401 — register tables
from vera_shared.graph.dedup import merge_entities


@pytest_asyncio.fixture
async def db(sqlite_db):
    # unique-индекс из миграции 017 — merge обязан работать при нём
    async with sqlite_db() as s:
        await s.execute(text(
            "CREATE UNIQUE INDEX uq_relationships_spo ON relationships "
            "(subject_entity_id, predicate, object_entity_id)"))
    yield sqlite_db


async def _seed_entities(get_session, n: int) -> list[int]:
    ids = []
    async with get_session() as s:
        for i in range(n):
            r = await s.execute(text(
                "INSERT INTO entities (type, name, attributes) "
                f"VALUES ('person', 'p{i}', '{{}}') RETURNING id"))
            ids.append(r.scalar())
    return ids


async def _rel(get_session, subj: int, obj: int, pred: str = "coworker_of"):
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO relationships (subject_entity_id, predicate, "
            "object_entity_id, confidence, is_current, fact) "
            "VALUES (:s, :p, :o, 0.9, 1, 'f')"),
            {"s": subj, "p": pred, "o": obj})


async def _all_rels(get_session) -> list[tuple]:
    async with get_session() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT subject_entity_id, predicate, object_entity_id "
            "FROM relationships ORDER BY 1, 2, 3"))).all()]


@pytest.mark.asyncio
async def test_merge_does_not_create_self_loop(db):
    a, b = await _seed_entities(db, 2)
    await _rel(db, b, a)                 # merged → keeper
    await merge_entities(keeper_id=a, merged_id=b)
    rels = await _all_rels(db)
    assert rels == []                    # петля a→a не создана, связь удалена


@pytest.mark.asyncio
async def test_merge_dedupes_parallel_relationships(db):
    a, b, c = await _seed_entities(db, 3)
    await _rel(db, a, c)                 # у keeper уже есть a→c
    await _rel(db, b, c)                 # у merged такая же связь b→c
    await merge_entities(keeper_id=a, merged_id=b)
    assert await _all_rels(db) == [(a, "coworker_of", c)]   # одна, не две


@pytest.mark.asyncio
async def test_merge_moves_unique_relationships_both_sides(db):
    a, b, c, d = await _seed_entities(db, 4)
    await _rel(db, b, c)                 # subject-сторона
    await _rel(db, d, b, "works_at")     # object-сторона
    await merge_entities(keeper_id=a, merged_id=b)
    assert await _all_rels(db) == [(a, "coworker_of", c), (d, "works_at", a)]


# ─── media-worker: guard от двойного append ────────────────────────────────


def test_on_success_sql_guards_status():
    import inspect

    import media_worker.repository as repo
    src = inspect.getsource(repo._on_success)
    assert "triage_status = 'media_pending'" in src
    src_fail = inspect.getsource(repo._on_failure)
    assert src_fail.count("triage_status = 'media_pending'") == 2


# ─── remember(): эмбеддинг пишется сразу ───────────────────────────────────


def test_remember_writes_embedding_immediately():
    import inspect

    from gateway import claude
    src = inspect.getsource(claude.remember)
    assert "INSERT INTO event_embeddings" in src

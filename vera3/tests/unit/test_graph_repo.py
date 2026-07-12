"""vera_shared.graph.repo — L1/L3 substrate writes.

Most graph_repo functions need a real Postgres (uses JSONB ? operator,
ON CONFLICT semantics, etc.). Smoke-test the import path here; real
behavior is integration-tested.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from vera_shared.db import models_graph
from vera_shared.db.engine import Base


def test_module_imports():
    from vera_shared.graph import repo
    # Public API
    assert hasattr(repo, "upsert_entity")
    assert hasattr(repo, "upsert_membership")
    assert hasattr(repo, "upsert_relationship")
    assert hasattr(repo, "upsert_identity_node")
    assert hasattr(repo, "find_entity_by_name")
    assert hasattr(repo, "find_entity_by_alias")
    assert hasattr(repo, "list_members")
    assert hasattr(repo, "get_style_for_listener")
    assert hasattr(repo, "get_global_style")


def test_models_graph_module_imports():
    """All ORM rows declared in models_graph are accessible."""
    from vera_shared.db import models_graph as m
    assert hasattr(m, "EntityRow")
    assert hasattr(m, "EntityAliasRow")
    assert hasattr(m, "MembershipRow")
    assert hasattr(m, "RelationshipRow")
    assert hasattr(m, "PatternRow")
    assert hasattr(m, "IdentityNodeRow")


def test_entity_row_tablename():
    from vera_shared.db.models_graph import EntityRow
    assert EntityRow.__tablename__ == "entities"


def test_entity_alias_unique_constraint_declared():
    from vera_shared.db.models_graph import EntityAliasRow
    constraints = [c.name for c in EntityAliasRow.__table_args__
                    if hasattr(c, "name")]
    assert "uq_alias_source_identifier" in constraints


def test_membership_unique_constraint_declared():
    from vera_shared.db.models_graph import MembershipRow
    constraints = [c.name for c in MembershipRow.__table_args__
                    if hasattr(c, "name")]
    assert "uq_membership" in constraints


def test_identity_node_payload_default_dict():
    from vera_shared.db.models_graph import IdentityNodeRow
    row = IdentityNodeRow(
        type="style",
        label="Style for Маша",
    )
    assert row.label == "Style for Маша"
    assert row.type == "style"


@pytest_asyncio.fixture
async def sqlite_repo(tmp_path):
    """Real file-based SQLite so repo functions' get_session() works — same
    reset pattern as tests/service/test_gateway.py."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'graph.db'}"
    import vera_shared.db.engine as engine_mod
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None
    from vera_shared.db.engine import init_engine
    engine = await init_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from vera_shared.graph import repo
    yield repo
    await engine.dispose()
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None


@pytest.mark.asyncio
async def test_resolve_entity_exact_by_name_and_alias(sqlite_repo):
    repo = sqlite_repo
    # NOTE: names are ASCII here on purpose — SQLite's built-in lower() only
    # folds ASCII, so case-insensitive Cyrillic can't be exercised on the
    # test DB (Postgres LOWER() handles it in prod; the old raw SQL had the
    # exact same limitation). Cyrillic is covered by the exact-case assert.
    eid = await repo.upsert_entity(
        type="person", name="Dmitry", source="telegram", identifier="169",
        display_name="Dima Z",
    )
    cyr = await repo.upsert_entity(
        type="person", name="Мария", source="telegram", identifier="170",
    )
    # exact name, case-insensitive (ASCII), NOT fuzzy substring
    assert await repo.resolve_entity_exact("dmitry") == eid
    assert await repo.resolve_entity_exact("DMITRY") == eid
    # via alias display_name
    assert await repo.resolve_entity_exact("dima z") == eid
    # Cyrillic exact-case works (no lowering needed)
    assert await repo.resolve_entity_exact("Мария") == cyr
    # partial substring must NOT match — it's exact, not ILIKE %..%
    assert await repo.resolve_entity_exact("Dmit") is None
    assert await repo.resolve_entity_exact("nobody") is None


@pytest.mark.asyncio
async def test_upsert_relationship_returns_true_then_false(sqlite_repo):
    repo = sqlite_repo
    a = await repo.upsert_entity(type="person", name="A", source="s", identifier="a")
    b = await repo.upsert_entity(type="org", name="B", source="s", identifier="b")

    first = await repo.upsert_relationship(
        subject_entity_id=a, object_entity_id=b, predicate="works_at",
        fact=None, confidence=0.5, derived_from_event_id=1,
    )
    assert first is True   # genuinely inserted

    # same tuple again → soft-upsert, back-fills fact, raises confidence
    second = await repo.upsert_relationship(
        subject_entity_id=a, object_entity_id=b, predicate="works_at",
        fact="works at B", confidence=0.9,
    )
    assert second is False   # not a new row


async def _triangle(repo):
    """A is the hub (degree 2): A→B works_at, A→C friend_of."""
    a = await repo.upsert_entity(type="person", name="A", source="s", identifier="a")
    b = await repo.upsert_entity(type="org", name="B", source="s", identifier="b")
    c = await repo.upsert_entity(type="person", name="C", source="s", identifier="c")
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="works_at", confidence=0.8)
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=c,
                                   predicate="friend_of", confidence=0.7)
    return a, b, c


@pytest.mark.asyncio
async def test_graph_snapshot_core_respects_min_degree(sqlite_repo):
    repo = sqlite_repo
    a, b, c = await _triangle(repo)

    # min_degree=1 → all three nodes, both edges
    snap = await repo.graph_snapshot(min_degree=1, limit=100)
    assert {n["id"] for n in snap["nodes"]} == {a, b, c}
    assert len(snap["edges"]) == 2
    # node carries degree + type for the viz
    hub = next(n for n in snap["nodes"] if n["id"] == a)
    assert hub["degree"] == 2 and hub["type"] == "person"

    # min_degree=2 → only the hub qualifies; its edges point to excluded
    # nodes, so no edge survives the both-endpoints-present filter
    snap2 = await repo.graph_snapshot(min_degree=2, limit=100)
    assert [n["id"] for n in snap2["nodes"]] == [a]
    assert snap2["edges"] == []


@pytest.mark.asyncio
async def test_graph_snapshot_focus_is_ego_network(sqlite_repo):
    repo = sqlite_repo
    a, b, c = await _triangle(repo)
    snap = await repo.graph_snapshot(focus_id=a, limit=100)
    assert {n["id"] for n in snap["nodes"]} == {a, b, c}
    assert len(snap["edges"]) == 2


@pytest.mark.asyncio
async def test_graph_snapshot_predicate_filter(sqlite_repo):
    repo = sqlite_repo
    a, b, c = await _triangle(repo)
    snap = await repo.graph_snapshot(min_degree=1, limit=100, predicate="works_at")
    assert len(snap["edges"]) == 1
    assert snap["edges"][0]["predicate"] == "works_at"


@pytest.mark.asyncio
async def test_graph_snapshot_empty_when_no_relationships(sqlite_repo):
    repo = sqlite_repo
    await repo.upsert_entity(type="person", name="Lonely", source="s", identifier="z")
    snap = await repo.graph_snapshot(min_degree=1, limit=100)
    assert snap == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_metadata_create_all_compiles_on_sqlite():
    """Regression: JSONB (postgres-only) без .with_variant(JSON, "sqlite")
    ломает ЛЮБОЙ тест что делает Base.metadata.create_all() на SQLite —
    даже тесты не связанные с graph-слоем (напр. gateway service tests),
    т.к. create_all проходит по ВСЕЙ shared Base.metadata, а не только по
    таблицам своего сервиса. entities/memberships/patterns/identity_nodes
    все несут JSONB-колонки — этот тест реально их создаёт на SQLite,
    а не просто импортирует модуль."""
    assert models_graph.EntityRow.__table__.name == "entities"  # registers on Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()

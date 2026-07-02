"""vera_shared.graph.repo — L1/L3 substrate writes.

Most graph_repo functions need a real Postgres (uses JSONB ? operator,
ON CONFLICT semantics, etc.). Smoke-test the import path here; real
behavior is integration-tested.
"""
from __future__ import annotations

import pytest


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


@pytest.mark.asyncio
async def test_metadata_create_all_compiles_on_sqlite():
    """Regression: JSONB (postgres-only) без .with_variant(JSON, "sqlite")
    ломает ЛЮБОЙ тест что делает Base.metadata.create_all() на SQLite —
    даже тесты не связанные с graph-слоем (напр. gateway service tests),
    т.к. create_all проходит по ВСЕЙ shared Base.metadata, а не только по
    таблицам своего сервиса. entities/memberships/patterns/identity_nodes
    все несут JSONB-колонки — этот тест реально их создаёт на SQLite,
    а не просто импортирует модуль."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from vera_shared.db import models_graph  # noqa: F401  — регистрирует таблицы на Base
    from vera_shared.db.engine import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()

"""vera_shared.graph.identity — name canonicalisation, candidate pairs,
suggestion storage (real SQLite, same fixture pattern as test_graph_avatars),
plus clusters.label_propagation (pure) and the strict namesake resolve."""
from __future__ import annotations

import pytest
import pytest_asyncio
from vera_shared.db import (
    models,  # noqa: F401  — registers events table on Base
    models_graph,  # noqa: F401  — registers graph tables (incl. merge_suggestions)
)
from vera_shared.db.engine import Base
from vera_shared.graph.identity import canonical_name_parts

# ─── canonical_name_parts ───────────────────────────────────────────────────


def test_diminutive_and_translit_fold_to_same_first():
    assert canonical_name_parts("Маша")[0] == "мария"
    assert canonical_name_parts("Мария Иванова")[0] == "мария"
    assert canonical_name_parts("Masha")[0] == "мария"
    assert canonical_name_parts("Оля")[0] == "ольга"
    assert canonical_name_parts("Ольга Олеговая")[0] == "ольга"
    assert canonical_name_parts("Olga")[0] == "ольга"
    assert canonical_name_parts("Дима 🏝️")[0] == "дмитрий"
    assert canonical_name_parts("Dima Zaporozhets")[0] == "дмитрий"


def test_last_name_translit_fold():
    _, last_cyr = canonical_name_parts("Мария Иванова")
    _, last_lat = canonical_name_parts("Maria Ivanova")
    assert last_cyr == last_lat == "иванова"


def test_empty_and_garbage_names():
    assert canonical_name_parts(None) == ("", "")
    assert canonical_name_parts("🏝️💥") == ("", "")
    assert canonical_name_parts("  ") == ("", "")


# ─── DB-backed: candidates + suggestions ────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'id.db'}"
    import vera_shared.db.engine as engine_mod
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None
    from vera_shared.db.engine import init_engine
    engine = await init_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None


async def _person(name, ident, source="telegram", **attrs):
    from vera_shared.graph import repo
    return await repo.upsert_entity(
        type="person", name=name, source=source, identifier=ident,
        attributes=attrs or {},
    )


@pytest.mark.asyncio
async def test_candidates_cross_source_pair_found(db):
    from vera_shared.graph.identity import find_identity_candidates
    a = await _person("Маша", "user:1", source="telegram", tg_id=1)
    b = await _person("Maria Ivanova", "maria@x.com", source="gmail")
    await _person("Пётр", "user:9", source="telegram", tg_id=9)   # not a match

    pairs = await find_identity_candidates(limit=10)
    assert (min(a, b), max(a, b), "разные каналы (кросс-источник)") in pairs


@pytest.mark.asyncio
async def test_candidates_same_source_needs_shared_chat_or_lastname(db):
    from vera_shared.graph import repo
    from vera_shared.graph.identity import find_identity_candidates
    a = await _person("Оля", "user:1", tg_id=1)
    b = await _person("Ольга Олеговая", "user:2", tg_id=2)
    # same source, no shared chat, no matching last name → NOT a candidate
    assert await find_identity_candidates(limit=10) == []

    chat = await repo.upsert_entity(type="group", name="Чат",
                                    source="telegram", identifier="chat:-1")
    await repo.upsert_membership(parent_entity_id=chat, child_entity_id=a,
                                 source="telegram")
    await repo.upsert_membership(parent_entity_id=chat, child_entity_id=b,
                                 source="telegram")
    pairs = await find_identity_candidates(limit=10)
    assert (min(a, b), max(a, b), "общие чаты") in pairs


@pytest.mark.asyncio
async def test_judged_pairs_never_reasked_and_status_flow(db):
    from vera_shared.graph.identity import (
        find_identity_candidates,
        list_pending_suggestions,
        save_suggestion,
        set_suggestion_status,
    )
    a = await _person("Маша", "user:1", source="telegram", tg_id=1)
    b = await _person("Masha", "m@x.com", source="gmail")

    await save_suggestion(a, b, {"verdict": "same", "confidence": 0.9,
                                 "reason": "одинаковый стиль"})
    assert await find_identity_candidates(limit=10) == []   # judged → excluded

    pending = await list_pending_suggestions()
    assert len(pending) == 1 and pending[0]["verdict"] == "same"

    row = await set_suggestion_status(pending[0]["id"], "accepted")
    assert {row["entity_a"], row["entity_b"]} == {a, b}
    assert await list_pending_suggestions() == []


@pytest.mark.asyncio
async def test_different_verdict_stored_pre_rejected(db):
    from vera_shared.graph.identity import (
        list_pending_suggestions,
        save_suggestion,
    )
    a = await _person("Дима", "user:1", tg_id=1)
    b = await _person("Дима", "d@x.com", source="gmail")
    await save_suggestion(a, b, {"verdict": "different", "confidence": 0.8,
                                 "reason": "разные темы"})
    assert await list_pending_suggestions() == []   # never shown, never re-asked


# ─── strict namesake resolve (rel_extract data-quality fix) ─────────────────


@pytest.mark.asyncio
async def test_resolve_entity_exact_refuses_ambiguous_namesakes(db):
    # ASCII on purpose: SQLite lower() folds ASCII only (same caveat as
    # test_graph_repo). Postgres LOWER() handles Cyrillic in prod.
    from vera_shared.graph import repo
    only = await _person("Unicum", "user:1", tg_id=1)
    assert await repo.resolve_entity_exact("unicum") == only

    await _person("Dima", "user:2", tg_id=2)
    await _person("Dima", "user:3", tg_id=3)
    assert await repo.resolve_entity_exact("Dima") is None   # ambiguous → skip


# ─── clusters.label_propagation (pure) ──────────────────────────────────────


def test_label_propagation_two_communities():
    from vera_shared.graph.clusters import label_propagation
    # two triangles bridged by one weak edge
    nodes = [1, 2, 3, 10, 11, 12]
    edges = [(1, 2), (2, 3), (1, 3), (10, 11), (11, 12), (10, 12), (3, 10)]
    assign = label_propagation(nodes, edges)
    assert assign[1] == assign[2] == assign[3]
    assert assign[10] == assign[11] == assign[12]
    # communities renumbered from 0 by size
    assert set(assign.values()) <= {0, 1}


def test_label_propagation_deterministic():
    from vera_shared.graph.clusters import label_propagation
    nodes = list(range(30))
    edges = [(i, (i + 1) % 15) for i in range(15)] + \
            [(15 + i, 15 + (i + 1) % 15) for i in range(15)]
    a = label_propagation(nodes, edges)
    b = label_propagation(nodes, edges)
    assert a == b

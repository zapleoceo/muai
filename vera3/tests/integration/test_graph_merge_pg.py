"""merge_entities / unmerge на живом Postgres.

SQLite не проверяет внешние ключи и не знает про уникальный индекс миграции
017, поэтому юнит-тесты не ловят главного: порядок операций, при котором
настоящая БД не нарушает ни FK, ни UNIQUE (subject, predicate, object), и
возврат удалённых сущностей с прежними id при живой последовательности.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy import text as sa_text
from vera_shared.timeutil import utc_naive_now

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vera:test@localhost:5433/vera_test",
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-integration")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import models, models_graph, models_sources  # noqa: F401
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
        # В проде индекс ставит миграция 017, ORM его не знает.
        await conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_relationships_spo "
            "ON relationships (subject_entity_id, predicate, object_entity_id)"))
    yield get_session
    await close_engine()


async def _world(get_session):
    from vera_shared.db.models import EventRow
    from vera_shared.db.models_graph import (
        EntityAliasRow,
        EntityAvatarRow,
        EntityRow,
        IdentityNodeRow,
        MembershipRow,
        RelationshipRow,
    )
    now = utc_naive_now()
    ids = {}
    async with get_session() as s:
        event = EventRow(source="telegram", source_event_id="e1", content_text="x",
                         occurred_at=now, received_at=now, triage_status="done")
        s.add(event)
        for key, type_ in (("keep", "person"), ("drop", "person"), ("chat", "supergroup"),
                           ("org", "organization")):
            row = EntityRow(type=type_, name=key, attributes={"k": key})
            s.add(row)
            await s.flush()
            ids[key] = row.id
        await s.flush()
        k, d, c, o = (ids[n] for n in ("keep", "drop", "chat", "org"))
        s.add_all([
            EntityAliasRow(entity_id=d, source="gmail", identifier="a@itstep.org"),
            MembershipRow(parent_entity_id=c, child_entity_id=k, source="telegram"),
            MembershipRow(parent_entity_id=c, child_entity_id=d, source="telegram"),
            RelationshipRow(subject_entity_id=k, object_entity_id=o, predicate="WORKS_AT",
                            confidence=0.4),
            RelationshipRow(subject_entity_id=d, object_entity_id=o, predicate="WORKS_AT",
                            confidence=0.9, derived_from_event_id=event.id),
            RelationshipRow(subject_entity_id=k, object_entity_id=d, predicate="SAME_AS"),
            EntityAvatarRow(entity_id=d, image=b"\x00\xff\x10", mime="image/png"),
            IdentityNodeRow(type="style", label="tone", listener_entity_id=d),
        ])
    return ids


@pytest.mark.asyncio
async def test_merge_respects_foreign_keys_and_unique_edge_index(pg_db):
    from vera_shared.db.models_graph import (
        EntityAvatarRow,
        EntityRow,
        IdentityNodeRow,
        RelationshipRow,
    )
    from vera_shared.graph.merge import merge_entities

    ids = await _world(pg_db)
    await merge_entities(ids["keep"], [ids["drop"]], "integration")
    async with pg_db() as s:
        assert (await s.get(EntityRow, ids["drop"])) is None
        rels = (await s.execute(select(RelationshipRow))).scalars().all()
        assert len(rels) == 1 and rels[0].confidence == 0.9
        assert rels[0].derived_from_event_id is not None  # FK на events пережил слияние
        node = (await s.execute(select(IdentityNodeRow))).scalar_one()
        assert node.listener_entity_id == ids["keep"]  # не обнулён каскадом SET NULL
        assert (await s.get(EntityAvatarRow, ids["keep"])).image == b"\x00\xff\x10"


@pytest.mark.asyncio
async def test_unmerge_restores_ids_and_sequence_stays_usable(pg_db):
    from vera_shared.db.models_graph import EntityAliasRow, EntityRow, RelationshipRow
    from vera_shared.graph.merge import merge_entities
    from vera_shared.graph.unmerge import unmerge

    ids = await _world(pg_db)
    report = await merge_entities(ids["keep"], [ids["drop"]], "integration")
    await unmerge(report.to_dict())
    async with pg_db() as s:
        assert (await s.get(EntityRow, ids["drop"])).name == "drop"
        alias = (await s.execute(select(EntityAliasRow))).scalar_one()
        assert alias.entity_id == ids["drop"]
        assert len((await s.execute(select(RelationshipRow))).scalars().all()) == 3
        fresh = EntityRow(type="person", name="after", attributes={})
        s.add(fresh)
        await s.flush()
        assert fresh.id not in ids.values()


@pytest.mark.asyncio
async def test_multiple_drops_with_duplicate_memberships_and_suggestions(pg_db):
    from vera_shared.db.models_graph import (
        EntityRow,
        MembershipRow,
        MergeSuggestionRow,
    )
    from vera_shared.graph.merge import merge_entities
    from vera_shared.graph.unmerge import unmerge

    ids = await _world(pg_db)
    async with pg_db() as s:
        extra = EntityRow(type="person", name="drop2", attributes={})
        s.add(extra)
        await s.flush()
        ids["drop2"] = extra.id
        s.add_all([
            MembershipRow(parent_entity_id=ids["chat"], child_entity_id=extra.id,
                          source="telegram"),
            MergeSuggestionRow(entity_a=ids["keep"], entity_b=ids["org"], verdict="same"),
            MergeSuggestionRow(entity_a=ids["drop"], entity_b=ids["org"], verdict="same"),
            MergeSuggestionRow(entity_a=ids["org"], entity_b=extra.id, verdict="unsure"),
        ])
    report = await merge_entities(ids["keep"], [ids["drop"], ids["drop2"]], "integration")
    async with pg_db() as s:
        mems = (await s.execute(select(MembershipRow))).scalars().all()
        sugs = (await s.execute(select(MergeSuggestionRow))).scalars().all()
    assert len(mems) == 1 and mems[0].child_entity_id == ids["keep"]
    assert len(sugs) == 1  # uq_merge_pair не нарушен, дубли склеены
    await unmerge(report)
    async with pg_db() as s:
        assert len((await s.execute(select(MembershipRow))).scalars().all()) == 3
        assert len((await s.execute(select(MergeSuggestionRow))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_avatar_conflict_keeps_real_photo_and_unmerge_restores_both(pg_db):
    from vera_shared.db.models_graph import EntityAvatarRow
    from vera_shared.graph.merge import merge_entities
    from vera_shared.graph.unmerge import unmerge

    ids = await _world(pg_db)
    async with pg_db() as s:
        s.add(EntityAvatarRow(entity_id=ids["keep"], image=None, missing=True))
    report = await merge_entities(ids["keep"], [ids["drop"]], "integration")
    async with pg_db() as s:
        av = (await s.execute(select(EntityAvatarRow))).scalar_one()
        assert av.entity_id == ids["keep"] and av.image == b"\x00\xff\x10"
    await unmerge(report)
    async with pg_db() as s:
        rows = {a.entity_id: a for a in (await s.execute(select(EntityAvatarRow))).scalars()}
    assert rows[ids["keep"]].missing is True and rows[ids["drop"]].image == b"\x00\xff\x10"


@pytest.mark.asyncio
async def test_undo_after_partial_apply(pg_db, tmp_path, monkeypatch):
    from vera_shared.db.models_graph import EntityAliasRow, EntityRow
    from vera_shared.graph import dupe_apply
    from vera_shared.graph.dupe_apply import apply_plan, undo_report
    from vera_shared.graph.dupe_detect import build_plan
    from vera_shared.graph.dupe_snapshot import load_snapshot
    await _world(pg_db)
    # Три независимых сервисных отправителя → ровно три retype в плане; без
    # этого план из одного действия никогда не доходил до точки отказа.
    async with pg_db() as s:
        for n, domain in enumerate(("alpha-corp.com", "beta-corp.com", "gamma-corp.com")):
            ent = EntityRow(type="person", name=f"Brand{n}", attributes={})
            s.add(ent)
            await s.flush()
            s.add(EntityAliasRow(entity_id=ent.id, source="gmail",
                                 identifier=f"no-reply@{domain}"))
    plan = dupe_apply.plan_document(build_plan(await load_snapshot()), "pg")
    real, calls = dupe_apply._exec, []

    async def flaky(action, s):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("partial")
        return await real(action, s)

    monkeypatch.setattr(dupe_apply, "_exec", flaky)
    async with pg_db() as s:
        before = (await s.execute(sa_text("SELECT id, type, name FROM entities ORDER BY id"))).all()
    assert sum(1 for x in plan["actions"] if x["action"] == "retype") == 3
    with pytest.raises(RuntimeError):
        await apply_plan(plan, tmp_path / "r.json")
    assert len(calls) == 2
    assert await undo_report(tmp_path / "r.json") == 1
    # первое действие откатано, вторая транзакция не закоммичена
    async with pg_db() as s:
        after = (await s.execute(sa_text("SELECT id, type, name FROM entities ORDER BY id"))).all()
    assert after == before

"""Разрыв связи из дашборда: гашение ролей, отвергнутое «работает с», откат, учёт в модели.

Данные синтетические; SQLite.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import PairStatsRow, RelationshipRow
from vera_shared.db.models_mcp import McpAuditRow
from vera_shared.graph import connections, pair_stats, repo
from vera_shared.graph.connection_actions import break_role, reject_inferred
from vera_shared.graph.edit import GraphEditError
from vera_shared.journal.undo import UndoRefused, undo_entry

pytestmark = pytest.mark.asyncio


async def person(name: str, ident: str, email: str | None = None) -> int:
    return await repo.upsert_entity(
        type="person", name=name, source="telegram", identifier=f"user:{ident}",
        attributes={"email": email} if email else None)


async def spouse_rel(a: int, b: int) -> list[int]:
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="spouse_of", confidence=0.9,
                                   derived_from_event_id=None)
    async with get_session() as s:
        rows = (await s.execute(select(RelationshipRow.id))).scalars().all()
    return list(rows)


async def work_stats(a: int, b: int) -> None:
    low, high = pair_stats.ordered(a, b)
    async with get_session() as s:
        s.add(PairStatsRow(entity_a=low, entity_b=high, active_days=20, work_co_days=20))


async def audit_entries() -> list[McpAuditRow]:
    async with get_session() as s:
        return list((await s.execute(select(McpAuditRow).order_by(McpAuditRow.id))).scalars())


async def test_break_role_retires_rows_and_journals_as_dashboard(sqlite_db):
    a, b = await person("Игорь Тестов", "1"), await person("Лиза Ветрова", "2")
    rel_ids = await spouse_rel(a, b)
    ids = await break_role(b, a, rel_ids, "dashboard")
    assert len(ids) == 1
    out = await connections.entity_connections(a)
    assert all(c["main"]["predicate"] != "spouse_of" for c in out)
    (entry,) = await audit_entries()
    assert (entry.client, entry.tool, entry.target_kind) == ("dashboard", "relationship_retire",
                                                              "relationship")
    assert entry.before["is_current"] is True and entry.after["is_current"] is False


async def test_break_role_refuses_foreign_or_retired_rows(sqlite_db):
    a, b, c = (await person("А Тестов", "1"), await person("Б Тестова", "2"),
               await person("В Тестов", "3"))
    rel_ids = await spouse_rel(a, b)
    with pytest.raises(GraphEditError, match="does not connect"):
        await break_role(a, c, rel_ids, "dashboard")
    await break_role(a, b, rel_ids, "dashboard")
    with pytest.raises(GraphEditError, match="already retired"):
        await break_role(a, b, rel_ids, "dashboard")
    with pytest.raises(GraphEditError):
        await break_role(a, b, [], "dashboard")


async def test_undo_of_break_restores_the_connection(sqlite_db):
    a, b = await person("Игорь Тестов", "1"), await person("Лиза Ветрова", "2")
    rel_ids = await spouse_rel(a, b)
    (audit_id,) = await break_role(a, b, rel_ids, "dashboard")
    async with get_session() as s:
        res = await undo_entry(s, audit_id, "dashboard", force=False)
    assert res["undone"] == audit_id
    out = await connections.entity_connections(a)
    assert out[0]["main"]["predicate"] == "spouse_of" or any(
        r["predicate"] == "spouse_of" for r in [out[0]["main"], *out[0]["also"]])


async def test_rejecting_inferred_work_removes_it_from_the_model(sqlite_db):
    a = await person("Игорь Тестов", "1", "i@corp.example")
    b = await person("Лиза Ветрова", "2", "l@corp.example")
    await work_stats(a, b)
    assert (await connections.entity_connections(a))[0]["main"]["inferred"] is True
    audit_id = await reject_inferred(b, a, "dashboard")
    assert audit_id is not None
    assert await connections.entity_connections(a) == [] or all(
        not c["main"]["inferred"] for c in await connections.entity_connections(a))
    edges = await connections.connections_among([a, b])
    assert all(not e["inferred"] for e in edges)
    assert await reject_inferred(a, b, "dashboard") is None


async def test_suppression_keeps_recorded_roles(sqlite_db):
    a, b = await person("Игорь Тестов", "1", "i@corp.example"), await person(
        "Лиза Ветрова", "2", "l@corp.example")
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="coworker_of", confidence=0.9,
                                   derived_from_event_id=None)
    await work_stats(a, b)
    await reject_inferred(a, b, "dashboard")
    main = (await connections.entity_connections(a))[0]["main"]
    assert main["predicate"] == "coworker_of" and main["inferred"] is False


async def test_undo_of_rejection_brings_the_inferred_role_back(sqlite_db):
    a = await person("Игорь Тестов", "1", "i@corp.example")
    b = await person("Лиза Ветрова", "2", "l@corp.example")
    await work_stats(a, b)
    audit_id = await reject_inferred(a, b, "dashboard")
    async with get_session() as s:
        await undo_entry(s, audit_id, "dashboard", force=False)
    assert (await connections.entity_connections(a))[0]["main"]["inferred"] is True
    async with get_session() as s:
        with pytest.raises(UndoRefused, match="already undone"):
            await undo_entry(s, audit_id, "dashboard", force=False)

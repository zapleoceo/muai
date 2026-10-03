"""Связь как пара поверх БД: карточка, граф, подсказка «тот же человек», чистка.

SQLite: чтение `pair_stats` и сборка связей переносимы; пересборка кэша — Postgres-only
SQL, она в integration/test_pair_stats_pg.py. Данные синтетические.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from vera_shared.db.models_graph import PairStatsRow
from vera_shared.graph import connections, pair_stats, repo, suggestions

pytestmark = pytest.mark.asyncio


async def person(name: str, ident: str, email: str | None = None) -> int:
    return await repo.upsert_entity(
        type="person", name=name, source="telegram", identifier=f"user:{ident}",
        attributes={"email": email} if email else None)


async def rel(a: int, predicate: str, b: int, *, conf: float = 0.8, event: int | None = 1) -> None:
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b, predicate=predicate,
                                   confidence=conf, derived_from_event_id=event)


async def stat(get_session, a: int, b: int, **kw: int) -> None:
    low, high = pair_stats.ordered(a, b)
    async with get_session() as s:
        s.add(PairStatsRow(entity_a=low, entity_b=high, **kw))


async def test_one_connection_per_counterpart_not_one_per_phrase(sqlite_db):
    owner, andrey = await person("Игорь Тестов", "1"), await person("Андрей", "2")
    await rel(andrey, "reports_to", owner, event=None)
    for predicate in ("client_of", "coworker_of", "vendor_of"):
        await rel(owner, predicate, andrey, event=len(predicate))
    out = await connections.entity_connections(owner)
    assert [c["other_id"] for c in out] == [andrey]
    only = out[0]
    assert only["main"]["predicate"] == "boss_of" and only["main"]["direction"] == "out"
    assert only["hidden"] + len(only["also"]) == 2
    assert only["also"] == []
    assert only["interaction"]["established"] is False


async def test_strong_contact_lifts_the_pair_and_is_reported(sqlite_db):
    owner = await person("Игорь Тестов", "1", "i@corp.example")
    lisa = await person("Лиза Ветрова", "2", "l@corp.example")
    await rel(lisa, "coworker_of", owner)
    await stat(sqlite_db, owner, lisa, dm_msgs=400, dm_days=40, active_days=40)
    quiet_owner = await person("Пётр Тихий", "3", "p@corp.example")
    quiet = await person("Нина Тихая", "4", "n@corp.example")
    await rel(quiet, "coworker_of", quiet_owner)
    busy = (await connections.entity_connections(owner))[0]
    calm = (await connections.entity_connections(quiet_owner))[0]
    assert busy["weight"] > calm["weight"] * 1.8
    assert busy["interaction"]["dm_msgs"] == 400 and busy["interaction"]["established"] is True
    assert busy["main"]["direction"] == "both"


async def test_work_together_is_inferred_without_any_phrase(sqlite_db):
    owner = await person("Игорь Тестов", "1", "igor@corp.example")
    lisa = await person("Лиза Ветрова", "2", "lisa@corp.example")
    stranger = await person("Олег Чужой", "3", "oleg@other.example")
    await stat(sqlite_db, owner, lisa, dm_days=30, active_days=30)
    await stat(sqlite_db, owner, stranger, dm_days=30, active_days=30)
    out = await connections.entity_connections(owner)
    by_id = {c["other_id"]: c for c in out}
    assert set(by_id) == {lisa, stranger}
    assert by_id[lisa]["main"]["predicate"] == "coworker_of" and by_id[lisa]["main"]["inferred"]
    assert by_id[lisa]["main"]["support"] == 0 and by_id[lisa]["shared_work"] is True
    assert by_id[stranger]["main"]["predicate"] == "contact"           # 30 дней личного общения


async def test_work_chats_alone_infer_work_only_with_enough_days(sqlite_db):
    a, b, c = await person("А Ааа", "1"), await person("Б Ббб", "2"), await person("В Ввв", "3")
    await stat(sqlite_db, a, b, co_days=6, work_co_days=6, active_days=6)
    await stat(sqlite_db, a, c, co_days=6, work_co_days=2, active_days=6)
    assert [x["other_id"] for x in await connections.entity_connections(a)] == [b]


async def test_fragment_gets_a_hint_from_a_pending_suggestion(sqlite_db):
    owner = await person("Игорь Тестов", "1")
    fragment = await person("Лиза", "2")
    full = await person("Елизавета Ветрова", "3")
    await rel(fragment, "friend_of", owner)
    assert await suggestions.propose_merge(fragment, full, confidence=0.8, reason="тест")
    (conn,) = await connections.entity_connections(owner)
    assert conn["possible_same"] == [{"id": full, "name": "Елизавета Ветрова",
                                      "reason": "suggestion"}]


async def test_fragment_gets_a_hint_from_a_strong_twin_with_the_same_first_name(sqlite_db):
    owner = await person("Игорь Тестов", "1")
    fragment = await person("Лиза", "2")
    twin = await person("Лиза Ветрова", "3")
    other = await person("Мария Другая", "4")
    await rel(fragment, "friend_of", owner)
    await rel(twin, "coworker_of", owner)
    await stat(sqlite_db, owner, twin, dm_days=40, active_days=40)
    await stat(sqlite_db, owner, other, dm_days=40, active_days=40)
    await rel(other, "coworker_of", owner)
    by_id = {c["other_id"]: c for c in await connections.entity_connections(owner)}
    assert by_id[fragment]["possible_same"] == [{"id": twin, "name": "Лиза Ветрова",
                                                 "reason": "twin"}]
    assert "possible_same" not in by_id[twin] and "possible_same" not in by_id[other]


async def test_twin_must_be_stronger_than_the_fragment(sqlite_db):
    owner = await person("Игорь Тестов", "1")
    fragment = await person("Лиза", "2")
    twin = await person("Лиза Ветрова", "3")
    await rel(fragment, "friend_of", owner)
    await rel(twin, "friend_of", owner)
    await stat(sqlite_db, owner, fragment, dm_days=40, active_days=40)
    await stat(sqlite_db, owner, twin, dm_days=10, active_days=10)
    by_id = {c["other_id"]: c for c in await connections.entity_connections(owner)}
    assert "possible_same" not in by_id[fragment]


async def test_graph_snapshot_draws_one_edge_per_pair(sqlite_db):
    a, b = await person("Игорь Тестов", "1"), await person("Андрей", "2")
    await rel(a, "boss_of", b, event=None)
    for predicate in ("client_of", "vendor_of"):
        await rel(a, predicate, b, event=len(predicate))
    snap = await repo.graph_snapshot(min_degree=1, limit=50)
    assert len(snap["edges"]) == 1
    edge = snap["edges"][0]
    assert {edge["source"], edge["target"]} == {a, b} and edge["weight"] > 0
    assert edge["predicate"] == "boss_of"
    raw = await repo.graph_snapshot(min_degree=1, limit=50, raw_edges=True)
    assert len(raw["edges"]) == 3


async def test_graph_snapshot_edge_points_from_boss_to_subordinate(sqlite_db):
    boss, sub = await person("Игорь Тестов", "1"), await person("Пётр Тихий", "2")
    await rel(sub, "reports_to", boss, event=None)
    (edge,) = (await repo.graph_snapshot(focus_id=sub, limit=10))["edges"]
    assert (edge["source"], edge["target"], edge["predicate"]) == (boss, sub, "boss_of")


async def test_graph_snapshot_predicate_filter_keeps_only_pairs_with_that_role(sqlite_db):
    a, b, c = await person("А Ааа", "1"), await person("Б Ббб", "2"), await person("В Ввв", "3")
    await rel(a, "boss_of", b, event=None)
    await rel(a, "friend_of", c)
    edges = (await repo.graph_snapshot(min_degree=1, limit=50, predicate="boss_of"))["edges"]
    assert [(e["source"], e["target"]) for e in edges] == [(a, b)]


async def test_graph_snapshot_includes_inferred_work_edges_and_hides_duplicate_member_of(sqlite_db):
    a = await person("А Ааа", "1", "a@corp.example")
    b = await person("Б Ббб", "2", "b@corp.example")
    await rel(a, "friend_of", b)
    await stat(sqlite_db, a, b, active_days=20)
    await repo.upsert_membership(parent_entity_id=a, child_entity_id=b, source="telegram")
    snap = await repo.graph_snapshot(min_degree=1, limit=50)
    assert len(snap["edges"]) == 1
    assert snap["edges"][0]["predicate"] in {"friend_of", "coworker_of"}
    only_work = await repo.graph_snapshot(min_degree=1, limit=50, predicate="coworker_of")
    assert not only_work["edges"] or only_work["edges"][0]["predicate"] == "coworker_of"


async def test_established_pairs_follow_interaction_strength(sqlite_db):
    a, b, c = await person("А Ааа", "1"), await person("Б Ббб", "2"), await person("В Ввв", "3")
    await stat(sqlite_db, a, b, active_days=30)
    await stat(sqlite_db, a, c, active_days=2)
    found = await connections.established_pairs([(b, a), (a, c), (b, c), (a, a)])
    assert found == {pair_stats.ordered(a, b)}


async def test_missing_pair_stats_table_degrades_to_recorded_roles(sqlite_db):
    a, b = await person("А Ааа", "1"), await person("Б Ббб", "2")
    await rel(a, "boss_of", b, event=None)
    async with sqlite_db() as s:
        await s.execute(text("DROP TABLE pair_stats"))
    out = await connections.entity_connections(a)
    assert out[0]["main"]["predicate"] == "boss_of"
    assert out[0]["interaction"]["active_days"] == 0
    assert await connections.established_pairs([(a, b)]) == set()


async def test_strong_contact_without_rows_is_on_the_card_and_in_the_ego_graph_only(sqlite_db):
    owner, friend = await person("Игорь Тестов", "1"), await person("Маша Тестова", "2")
    third = await person("Пётр Третий", "3")
    await stat(sqlite_db, owner, friend, dm_msgs=615, dm_days=108, active_days=108)
    await stat(sqlite_db, third, friend, co_days=12, active_days=12)
    (card,) = await connections.entity_connections(owner)
    assert card["other_id"] == friend and card["main"]["predicate"] == "contact"
    ego = await repo.graph_snapshot(focus_id=owner, limit=50)
    assert {n["id"] for n in ego["nodes"]} == {owner, friend}
    assert [(e["predicate"]) for e in ego["edges"]] == ["contact"]
    await rel(owner, "friend_of", third)
    core = await repo.graph_snapshot(min_degree=1, limit=50)
    assert all(e["predicate"] != "contact" for e in core["edges"])


async def test_contact_below_the_threshold_is_not_shown(sqlite_db):
    a, b = await person("А Ааа", "1"), await person("Б Ббб", "2")
    await stat(sqlite_db, a, b, dm_days=9, active_days=9)
    assert await connections.entity_connections(a) == []

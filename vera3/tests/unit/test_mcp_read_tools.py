"""MCP read-инструменты на SQLite: выдачи ограничены, скрытое не светится."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from vera_mcp import read_tools as r
from vera_mcp import write_tools as w
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import EntityAliasRow, EntityRow
from vera_shared.search_client import SearchUnavailable
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio


def ctx():
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": "claude"})))


async def seed_events() -> None:
    now = utc_naive_now()
    async with get_session() as s:
        s.add_all([
            EventRow(id=1, source="telegram", source_event_id="1", account="acc1",
                     content_text="hello from Alice", occurred_at=now - timedelta(hours=1),
                     project="itstep", triage_status="done",
                     metadata_={"sender_id": "42", "chat_title": "Team"}),
            EventRow(id=2, source="gmail", source_event_id="2", account="acc2",
                     content_text="invoice for Bob Marley", occurred_at=now - timedelta(hours=2),
                     triage_status="pending", metadata_={"from": "Bob <bob@example.com>"}),
            EventRow(id=3, source="telegram", source_event_id="3", account="acc1",
                     content_text="old message", occurred_at=now - timedelta(days=10),
                     triage_status="done", metadata_={"sender_id": "42"}),
        ])


async def seed_entities() -> tuple[int, int]:
    async with get_session() as s:
        alice = EntityRow(type="person", name="Alice Anderson",
                          attributes={"username": "alice_a", "email": "alice@corp.test"})
        bob = EntityRow(type="person", name="Bob Marley", attributes={})
        s.add_all([alice, bob])
        await s.flush()
        s.add_all([
            EntityAliasRow(entity_id=alice.id, source="telegram", identifier="user:42"),
            EntityAliasRow(entity_id=bob.id, source="gmail", identifier="bob@example.com",
                           display_name="Bobby"),
        ])
        return alice.id, bob.id


# ─── события ────────────────────────────────────────────────────────────────


async def test_recent_events_filters_and_cap(sqlite_db):
    await seed_events()
    everything = await r.recent_events(hours=48)
    assert [e["id"] for e in everything["events"]] == [1, 2]
    assert (await r.recent_events(hours=48, source="gmail"))["count"] == 1
    assert (await r.recent_events(hours=48, account="acc1"))["events"][0]["id"] == 1
    assert (await r.recent_events(hours=48, project="itstep"))["count"] == 1
    capped = await r.recent_events(hours=48, limit=1)
    assert capped["count"] == 1 and capped["truncated"] is True


async def test_hidden_event_leaves_recent_and_timeline_but_get_event_still_shows_it(sqlite_db):
    await seed_events()
    alice, _ = await seed_entities()
    await w.hide_event(1, ctx())
    assert [e["id"] for e in (await r.recent_events(hours=48))["events"]] == [2]
    tl = await r.timeline(alice, start="2020-01-01", end="2100-01-01")
    assert [e["id"] for e in tl["events"]] == [3]
    got = await r.get_event(1)
    assert got["hidden"] is True and got["triage"]["status"] == "hidden"
    await w.unhide_event(1, ctx())
    assert [e["id"] for e in (await r.recent_events(hours=48))["events"]] == [1, 2]


async def test_get_event_full_payload_and_truncation(sqlite_db):
    await seed_events()
    await seed_entities()
    got = await r.get_event(1, max_chars=100)
    assert got["source"] == "telegram" and got["metadata"]["chat_title"] == "Team"
    assert got["content_truncated"] is False
    assert got["entities"] == [{"entity_id": 1, "name": "Alice Anderson",
                                "type": "person", "via": "author"}]
    mail = await r.get_event(2)
    assert [e["name"] for e in mail["entities"]] == ["Bob Marley"]
    async with get_session() as s:
        (await s.get(EventRow, 1)).content_text = "x" * 500
    assert (await r.get_event(1, max_chars=100))["content_truncated"] is True
    with pytest.raises(LookupError):
        await r.get_event(404)


async def test_list_sources(sqlite_db):
    await seed_events()
    out = await r.list_sources()
    assert out["total_events"] == 3
    by = {s["source"]: s for s in out["sources"]}
    assert by["telegram"]["events"] == 2 and by["gmail"]["pending"] == 1
    assert by["telegram"]["last_ingested_at"]


async def test_timeline_by_alias_name_mention_and_period(sqlite_db):
    await seed_events()
    alice, bob = await seed_entities()
    week = await r.timeline(alice, start=(utc_naive_now() - timedelta(days=7)).isoformat())
    assert [e["id"] for e in week["events"]] == [1]
    wide = await r.timeline(alice, start="2020-01-01")
    assert [e["id"] for e in wide["events"]] == [1, 3]
    mail = await r.timeline(bob, start="2020-01-01")                 # адрес + имя в тексте
    assert [e["id"] for e in mail["events"]] == [2]
    capped = await r.timeline(alice, start="2020-01-01", limit=1)
    assert capped["truncated"] is True and len(capped["events"]) == 1
    assert (await r.timeline(999))["events"] == []


# ─── сущности и граф ────────────────────────────────────────────────────────


async def test_entity_find_by_name_alias_username_email(sqlite_db):
    alice, bob = await seed_entities()
    for query, expected in [("alice anderson", alice), ("alice_a", alice),
                            ("corp.test", alice), ("user:42", alice), ("bobby", bob),
                            ("bob@example", bob), ("MARLEY", bob)]:
        found = await r.entity_find(query)
        assert expected in [e["id"] for e in found["entities"]], query
    assert (await r.entity_find("zzzz-nothing"))["entities"] == []
    assert (await r.entity_find("alice", type="group"))["entities"] == []


async def test_entity_find_treats_percent_literally_and_ranks_exact_first(sqlite_db):
    alice, bob = await seed_entities()
    assert (await r.entity_find("%%"))["entities"] == []
    async with get_session() as s:
        s.add(EntityRow(type="person", name="Bob", attributes={}))
    names = [e["name"] for e in (await r.entity_find("bob"))["entities"]]
    assert names[0] == "Bob"
    capped = await r.entity_find("bob", limit=1)
    assert capped["truncated"] is True


async def test_entity_context_by_id_and_by_name(sqlite_db):
    alice, _ = await seed_entities()
    await w.relationship_set(alice, alice + 1, "coworker_of", ctx())
    by_id = await r.entity_context(entity_id=alice, raw_relationships=True)
    assert by_id["name"] == "Alice Anderson"
    assert {a["identifier"] for a in by_id["aliases"]} == {"user:42"}
    assert by_id["relationships"][0]["predicate"] == "coworker_of"
    assert by_id["relationships"][0]["id"]                            # нужен для relationship_retire
    assert by_id["connections"][0]["main"]["predicate"] == "coworker_of"
    assert by_id["members_truncated"] is False
    by_name = await r.entity_context(name="Alice")
    assert by_name["entity_id"] == alice
    with pytest.raises(ValueError, match="entity_id or name"):
        await r.entity_context()
    with pytest.raises(LookupError):
        await r.entity_context(name="Nobody At All")
    with pytest.raises(LookupError):
        await r.entity_context(entity_id=999)


async def test_entity_context_returns_connections_not_raw_rows_by_default(sqlite_db):
    alice, bob = await seed_entities()
    await w.relationship_set(alice, bob, "boss_of", ctx())
    out = await r.entity_context(entity_id=alice)
    assert "relationships" not in out
    (conn,) = out["connections"]
    assert conn["other_id"] == bob and conn["main"]["predicate"] == "boss_of"
    assert conn["main"]["rel_ids"]                                  # relationship_retire по-прежнему возможен


async def test_graph_neighbours_one_edge_per_pair_and_raw_flag(sqlite_db):
    alice, bob = await seed_entities()
    await w.relationship_set(alice, bob, "boss_of", ctx())
    await w.relationship_set(alice, bob, "client_of", ctx())
    assert len((await r.graph_neighbours(alice))["edges"]) == 1
    assert len((await r.graph_neighbours(alice, raw_edges=True))["edges"]) == 2


async def test_graph_neighbours(sqlite_db):
    alice, bob = await seed_entities()
    await w.relationship_set(alice, bob, "friend_of", ctx())
    out = await r.graph_neighbours(alice)
    assert {n["id"] for n in out["nodes"]} == {alice, bob}
    assert out["edges"][0]["predicate"] == "friend_of"
    assert (await r.graph_neighbours(alice, predicate="boss_of"))["edges"] == []


# ─── search, sql, audit ─────────────────────────────────────────────────────


async def test_search_proxies_brain_search_and_trims(sqlite_db, monkeypatch):
    monkeypatch.setenv("SEARCH_URL", "http://search.test")
    monkeypatch.setenv("INTERNAL_SECRET", "s3cret")
    payload = {"answer": "llm text", "results": [{"event_id": i} for i in range(5)]}
    fake = AsyncMock(return_value=payload)
    with patch("vera_mcp.read_tools.search_brain", fake):
        out = await r.search("budget", limit=3)
    fake.assert_awaited_once_with("http://search.test", "s3cret", "budget", 3)
    assert out == {"results": [{"event_id": 0}, {"event_id": 1}, {"event_id": 2}],
                   "count": 3, "truncated": True}
    assert "answer" not in out


async def test_search_unavailable_becomes_error(sqlite_db):
    boom = AsyncMock(side_effect=SearchUnavailable(502, "brain-search unreachable"))
    with patch("vera_mcp.read_tools.search_brain", boom), \
         pytest.raises(RuntimeError, match="unreachable"):
        await r.search("x")


async def test_sql_query_tool_and_audit_log(sqlite_db, ro_env):
    await seed_events()
    out = await r.sql_query("SELECT source, COUNT(*) AS n FROM events GROUP BY source ORDER BY n DESC")
    assert out["rows"][0] == ["telegram", 2]
    await w.hide_event(1, ctx())
    log = await r.audit_log()
    assert [e["tool"] for e in log["entries"]] == ["hide_event"]
    assert (await r.audit_log(client="other"))["entries"] == []
    assert datetime.fromisoformat(log["entries"][0]["at"])

"""Пересборка pair_stats на живом Postgres: jsonb-метаданные, регэксп адреса, FILTER,
INSERT … SELECT и advisory-замок. SQLite этого SQL не знает. Данные синтетические.
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vera:test@localhost:5433/vera_test",
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)

OWNER = 100


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-integration")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", str(OWNER))
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import models, models_graph, models_sources  # noqa: F401
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


async def _person(name: str, source: str, ident: str, kind: str = "person") -> int:
    from vera_shared.graph import repo
    return await repo.upsert_entity(type=kind, name=name, source=source, identifier=ident)


async def _event(get_session, n: int, source: str, day: int, **meta) -> None:
    from vera_shared.db.models import EventRow
    async with get_session() as s:
        s.add(EventRow(source=source, source_event_id=f"e{n}", content_text="x",
                       occurred_at=datetime(2026, 9, day, 10), triage_status="done",
                       metadata_=meta))


async def _stats(get_session) -> dict[tuple[int, int], dict]:
    async with get_session() as s:
        rows = (await s.execute(sa_text("SELECT * FROM pair_stats"))).mappings().all()
    return {(r["entity_a"], r["entity_b"]): dict(r) for r in rows}


async def _seed_group_day(get_session, chat: int, senders: list[int], day: int, base: int) -> None:
    for i, sender in enumerate(senders):
        await _event(get_session, base + i, "telegram", day, chat_type="channel",
                     chat_id=str(chat), sender_id=str(sender), direction="received")


@pytest.mark.asyncio
async def test_refresh_counts_dms_co_activity_work_chats_mail_and_groups(pg_db):
    from vera_shared.graph import pair_stats, repo
    owner = await _person("Владелец Тестов", "telegram", f"user:{OWNER}")
    lisa = await _person("Лиза Ветрова", "telegram", "user:200")
    andrey = await _person("Андрей Громов", "telegram", "user:300")
    org = await _person("Банк", "gmail", "noreply@bank.example", kind="organization")
    await repo.upsert_entity_linked(type="person", name="Лиза Ветрова", source="gmail",
                                    identifier="lisa@corp.example",
                                    known_as=[("telegram", "user:200")])

    for n, (day, direction, sender) in enumerate(
            [(1, "received", 200), (1, "sent", OWNER), (2, "received", 200), (5, "sent", OWNER)], 1):
        await _event(pg_db, n, "telegram", day, chat_type="user", chat_id="200",
                     sender_id=str(sender), direction=direction)
    await _event(pg_db, 90, "telegram", 3, chat_type="user", chat_id=str(OWNER),
                 sender_id=str(OWNER), direction="sent")                     # «Избранное»
    for day in (1, 2):
        await _seed_group_day(pg_db, -1001, [200, 300], day, 100 + day * 10)
    await _seed_group_day(pg_db, -1002, [200, 300], 3, 140)                  # не рабочий чат
    async with pg_db() as s:
        await s.execute(sa_text("INSERT INTO project_membership (project, kind, key, source) "
                                "VALUES ('itstep', 'chat', '1001', 'test')"))
    lisa_mail = {"from": "Lisa <lisa@corp.example>", "to": "owner@mine.example"}
    to_lisa = {"from": "owner@mine.example", "to": "Lisa <lisa@corp.example>"}
    for k, (day, direction, headers) in enumerate([
            (4, "received", lisa_mail), (5, "sent", to_lisa), (6, "received", lisa_mail)]):
        await _event(pg_db, 200 + k, "gmail", day, direction=direction, **headers)
    await _event(pg_db, 210, "gmail", 4, direction="received",
                 **{"from": "noreply@bank.example", "to": "owner@mine.example"})
    group = await repo.upsert_entity(type="supergroup", name="Чат А", source="telegram",
                                     identifier="chat:-1003")
    for child in (owner, lisa, andrey):
        await repo.upsert_membership(parent_entity_id=group, child_entity_id=child,
                                     source="telegram")

    assert await pair_stats.refresh_pair_stats() > 0
    stats = await _stats(pg_db)
    dm = stats[pair_stats.ordered(owner, lisa)]
    assert (dm["dm_msgs"], dm["dm_days"]) == (4, 3)
    assert (dm["mail_msgs"], dm["mail_days"]) == (3, 3)
    assert dm["shared_groups"] == 1 and dm["active_days"] == 5
    co = stats[pair_stats.ordered(lisa, andrey)]
    assert (co["co_days"], co["work_co_days"], co["co_chats"]) == (3, 2, 2)
    assert co["active_days"] == 3 and co["first_at"].day == 1 and co["last_at"].day == 3
    assert not any(org in pair for pair in stats)
    assert all(a < b for a, b in stats)


@pytest.mark.asyncio
async def test_large_chats_are_not_a_signal_and_rerun_replaces_rows(pg_db, monkeypatch):
    from vera_shared.graph import pair_stats
    a = await _person("А Ааа", "telegram", "user:11")
    b = await _person("Б Ббб", "telegram", "user:12")
    c = await _person("В Ввв", "telegram", "user:13")
    await _seed_group_day(pg_db, -2001, [11, 12, 13], 1, 300)
    monkeypatch.setattr(pair_stats, "MAX_CHAT_AUTHORS", 2)
    await pair_stats.refresh_pair_stats()
    assert await _stats(pg_db) == {}
    monkeypatch.setattr(pair_stats, "MAX_CHAT_AUTHORS", 3)
    await pair_stats.refresh_pair_stats()
    assert set(await _stats(pg_db)) == {(a, b), (a, c), (b, c)}
    await pair_stats.refresh_pair_stats()
    assert len(await _stats(pg_db)) == 3


@pytest.mark.asyncio
async def test_concurrent_refresh_is_skipped_by_the_advisory_lock(pg_db):
    from vera_shared.graph import pair_stats
    async with pg_db() as holder:
        await holder.execute(sa_text("SELECT pg_advisory_xact_lock(:k)"),
                             {"k": pair_stats.REFRESH_LOCK_KEY})
        assert await pair_stats.refresh_pair_stats() is None
    assert await pair_stats.refresh_pair_stats() == 0


@pytest.mark.asyncio
async def test_read_side_works_on_the_rebuilt_table(pg_db):
    from vera_shared.graph import connections, pair_stats
    a = await _person("Игорь Тестов", "telegram", "user:21")
    b = await _person("Лиза Ветрова", "telegram", "user:22")
    for day in range(1, 11):
        await _event(pg_db, day, "telegram", day, chat_type="user", chat_id="22",
                     sender_id="22", direction="received")
    await pair_stats.refresh_pair_stats(21)
    assert (await pair_stats.partner_stats(a))[b].dm_days == 10
    assert await connections.established_pairs([(a, b)]) == {pair_stats.ordered(a, b)}


@pytest.mark.asyncio
async def test_card_and_graph_infer_work_from_rebuilt_stats_on_postgres(pg_db):
    from vera_shared.graph import connections, pair_stats, repo
    owner = await repo.upsert_entity(type="person", name="Игорь Тестов", source="telegram",
                                     identifier="user:31", attributes={"email": "i@corp.example"})
    lisa = await repo.upsert_entity(type="person", name="Лиза Ветрова", source="telegram",
                                    identifier="user:32", attributes={"email": "l@corp.example"})
    for day in range(1, 8):
        await _event(pg_db, day, "telegram", day, chat_type="user", chat_id="32",
                     sender_id="32", direction="received")
    await pair_stats.refresh_pair_stats(31)
    (card,) = await connections.entity_connections(owner)
    assert card["other_id"] == lisa and card["main"]["predicate"] == "coworker_of"
    assert card["main"]["inferred"] and card["interaction"]["dm_days"] == 7
    snap = await repo.graph_snapshot(min_degree=1, limit=10, focus_id=owner)
    assert [(e["predicate"], e["inferred"]) for e in snap["edges"]] == [("coworker_of", True)]

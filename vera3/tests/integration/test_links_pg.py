"""Связи событий на живом Postgres: jsonb-метаданные (`->>`), EXISTS-фильтры, индекс по пачкам,
чтение участников, фильтр поиска, план чистки тёзок. SQLite этого SQL не гарантирует.
Данные синтетические."""
from __future__ import annotations

import os
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy import text as sa_text

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vera:test@localhost:5433/vera_test",
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)

OWNER = "100"


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-integration")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import (  # noqa: F401
        models,
        models_graph,
        models_links,
        models_sources,
    )
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


async def _world(get_session) -> dict:
    from vera_shared.db.models import EventRow
    from vera_shared.graph import repo
    from vera_shared.links.nicknames import add_nickname

    async def person(name: str, tg: str, email: str | None = None) -> int:
        eid = await repo.upsert_entity(type="person", name=name, source="telegram", identifier=f"user:{tg}",
                                       attributes={"username": f"u{tg}"})
        if email:
            await repo.upsert_entity_linked(type="person", name=name, source="gmail", identifier=email,
                                            known_as=[("telegram", f"user:{tg}")])
        return eid

    w = {"owner": await person("Dmitry Testov", OWNER, "owner@mine.example"),
         "lisa": await person("Лиза Ветрова", "200", "lisa@corp.example"),
         "boss": await person("Виктор Корчагин", "300")}
    async with get_session() as s:
        from vera_shared.db.models_graph import PairStatsRow
        from vera_shared.graph.pair_stats import ordered
        low, high = ordered(w["owner"], w["boss"])
        s.add(PairStatsRow(entity_a=low, entity_b=high, active_days=20))     # кандидат для исправления ASR
        await s.execute(sa_text("INSERT INTO project_membership (project, kind, key, source) "
                                "VALUES ('itstep', 'chat', '1005001', 'test')"))
    await add_nickname(w["boss"], "ВП", scope_kind="work", scope_ids=["project:itstep"])
    rows = [
        ("telegram", "привет", 1, {"chat_type": "user", "chat_id": "200", "sender_id": "200", "direction": "received"}, None, None),
        ("telegram", "ВП просил ознакомиться", 2, {"chat_type": "chat", "chat_id": "-1005001", "sender_id": "200"}, None, None),
        ("telegram", "ВП нужен для поездки", 3, {"chat_type": "chat", "chat_id": "-1009", "sender_id": "200"}, None, None),
        ("gmail", "Subject: план\n---\nтекст", 4, {"from": "Лиза <lisa@corp.example>", "to": "owner@mine.example"}, None, None),
        ("voice", "Выжимка созвона", 5, {"voices": ["Лиза Ветрова", "Собеседник 2"]},
         {"utterances": [{"speaker": "Собеседник 2", "text": "а"}]}, "Арчагин"),
    ]
    async with get_session() as s:
        for n, (source, body, day, meta, extra, transcript) in enumerate(rows):
            s.add(EventRow(source=source, source_event_id=f"x{n}", content_text=body,
                           occurred_at=datetime(2026, 9, day), triage_status="done",
                           metadata_=meta, content_extra=extra, transcript_text=transcript))
    return w


async def _index(get_session) -> None:
    from vera_shared.db.models import EventRow
    from vera_shared.links import index
    from vera_shared.links.context import ContextBuilder

    builder = ContextBuilder()
    await builder.build([])
    async with get_session() as s:
        rows = list((await s.execute(select(EventRow))).scalars())
    await index.index_events(rows, await index.load_resources(builder.owner), builder)


@pytest.mark.asyncio
async def test_index_filters_and_participants_on_postgres(pg_db):
    from vera_shared.links import read
    from vera_shared.links.filters import EventFilter

    w = await _world(pg_db)
    await _index(pg_db)
    both, _ = await read.filtered_events(EventFilter(participant_ids=(w["owner"], w["lisa"])))
    assert len(both) == 3                       # личка, письмо, созвон (Лиза названа слушателем)
    calls, _ = await read.filtered_events(EventFilter(kind="call", with_owner=True))
    assert [e["source"] for e in calls] == ["voice"]
    said, _ = await read.filtered_events(EventFilter(mentioned_ids=(w["boss"],)))
    assert {e["source"] for e in said} == {"telegram", "voice"} and len(said) == 2
    assert await read.mention_counts(w["boss"]) == {"in_scope": 2, "out_of_scope": 1}
    call_id = calls[0]["id"]
    people = await read.event_participants(call_id)
    assert [u["label"] for u in people["unresolved_speakers"]] == ["Собеседник 2"]
    events = await read.entity_events(w["boss"], datetime(2026, 9, 1), datetime(2026, 10, 1), 10, ("mentioned",))
    assert len(events) == 2 and events[0]["roles"] == ["mentioned"]
    pair = await read.co_occurrence(w["owner"], w["lisa"])
    assert pair["by_kind"] == {"call": 1, "email": 1, "message": 1}


@pytest.mark.asyncio
async def test_search_scope_clause_executes_against_events(pg_db):
    from brain_search.retrieval_filters import LinkScope, links_clause
    from vera_shared.links.filters import EventFilter

    w = await _world(pg_db)
    await _index(pg_db)
    flt = EventFilter(participant_ids=(w["lisa"],), with_owner=True, kind="email")
    clause, params = links_clause(LinkScope(flt, w["owner"]))
    async with pg_db() as s:
        ids = (await s.execute(sa_text(f"SELECT events.id FROM events WHERE TRUE{clause}"), params)).scalars().all()
        mail = (await s.execute(sa_text("SELECT id FROM events WHERE source = 'gmail'"))).scalar_one()
    assert ids == [mail]


@pytest.mark.asyncio
async def test_namesake_plan_loads_rows_on_postgres(pg_db):
    from vera_shared.graph import repo
    from vera_shared.links.namesake_plan import build_namesake_plan, load_rows

    w = await _world(pg_db)
    namesake = await repo.upsert_entity(type="person", name="Дима", source="telegram", identifier="user:999")
    async with pg_db() as s:
        event_id = (await s.execute(sa_text("SELECT id FROM events WHERE source_event_id = 'x1'"))).scalar_one()
    await repo.upsert_relationship(subject_entity_id=namesake, object_entity_id=w["lisa"], predicate="boss_of",
                                   confidence=0.8, fact="x", derived_from_event_id=event_id)
    rows = await load_rows([namesake])
    assert len(rows) == 1
    actions = await build_namesake_plan(rows)
    assert [a["rule"] for a in actions] == ["namesake_repoint"]
    assert actions[0]["after"]["subject_entity_id"] == w["owner"]


@pytest.mark.asyncio
async def test_cursor_runs_resume_on_postgres(pg_db):
    from vera_shared.links import index
    from vera_shared.links.context import ContextBuilder

    await _world(pg_db)
    builder = ContextBuilder()
    await builder.build([])
    res = await index.load_resources(builder.owner)
    top = await index.reset_cursors()
    seen = 0
    while (result := await index.run_batch(index.BACKFILL, res, builder, 2)).last_id is not None:
        seen += result.events
    assert seen == top == 5


@pytest.mark.asyncio
async def test_merge_moves_link_data_and_unmerge_restores_on_postgres(pg_db):
    from vera_shared.db.models_links import (
        EntityNicknameRow,
        EventEntityRow,
        VoiceSpeakerMapRow,
    )
    from vera_shared.graph import merge, repo, unmerge
    from vera_shared.links.nicknames import add_nickname
    from vera_shared.links.speakers import put_speaker

    w = await _world(pg_db)
    dup = await repo.upsert_entity(type="person", name="Витя Кронов", source="telegram", identifier="user:301")
    await add_nickname(dup, "ККК", scope_kind="work")
    async with pg_db() as s:
        call = (await s.execute(sa_text("SELECT id FROM events WHERE source = 'voice'"))).scalar_one()
        await put_speaker(s, call, "Собеседник 2", dup)
    await _index(pg_db)
    async with pg_db() as s:      # производные связи у дубля
        await s.execute(sa_text("UPDATE event_entities SET entity_id = :d WHERE entity_id = :l AND role = 'author'"),
                        {"d": dup, "l": w["lisa"]})
    report = await merge.merge_entities(w["boss"], [dup], "дубль")
    async with pg_db() as s:
        rows = (await s.execute(select(EventEntityRow))).scalars().all()
        assert not [r for r in rows if r.entity_id == dup]
        assert [r.entity_id for r in rows if r.source_of_link == "manual"] == [w["boss"]]
        assert {r.entity_id for r in (await s.execute(select(EntityNicknameRow))).scalars()} == {w["boss"]}
        assert {r.entity_id for r in (await s.execute(select(VoiceSpeakerMapRow))).scalars()} == {w["boss"]}
    await unmerge.unmerge(report)
    async with pg_db() as s:
        manual = (await s.execute(select(EventEntityRow).where(
            EventEntityRow.source_of_link == "manual"))).scalars().all()
        assert [r.entity_id for r in manual] == [dup]
        assert {r.entity_id for r in (await s.execute(select(VoiceSpeakerMapRow))).scalars()} == {dup}

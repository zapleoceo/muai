"""Регрессии прогона вопросов на реальных данных: текстовый отбор ДО лимита, участники созвона из
выжимки (фамилия, фраза-прозвище), пересчёт связей при добавлении прозвища, период вопросов.
Данные синтетические; сценарии зеркалят прод-находки."""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from vera_mcp import link_write_tools as lw
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import PairStatsRow
from vera_shared.db.models_links import EventEntityRow
from vera_shared.graph import repo
from vera_shared.graph.pair_stats import ordered
from vera_shared.links import index
from vera_shared.links.context import ContextBuilder
from vera_shared.links.eval_questions import CASES, run_case
from vera_shared.links.filters import EventFilter
from vera_shared.links.matcher import PersonNames
from vera_shared.links.names import NameResolver
from vera_shared.links.nicknames import add_nickname
from vera_shared.links.read import filtered_events

pytestmark = pytest.mark.asyncio
OWNER_TG = "100"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER_TG)


async def person(name: str, tg: str) -> int:
    return await repo.upsert_entity(type="person", name=name, source="telegram", identifier=f"user:{tg}")


async def event(gs, n: int, source: str, body: str, day: int = 1, **kw) -> int:
    async with gs() as s:
        row = EventRow(source=source, source_event_id=f"pe{n}", content_text=body,
                       occurred_at=datetime(2026, 8, 1 + day % 25), triage_status="done",
                       metadata_=kw.pop("meta", {}), **kw)
        s.add(row)
        await s.flush()
        return row.id


@pytest_asyncio.fixture
async def world(sqlite_db):
    gs = sqlite_db
    w = {"gs": gs, "owner": await person("Dmitry Testov", OWNER_TG),
         "director": await person("Дмитрий Корчевский", "300"), "lisa": await person("Лиза Ветрова", "200")}
    async with gs() as s:
        low, high = ordered(w["owner"], w["director"])
        s.add(PairStatsRow(entity_a=low, entity_b=high, active_days=30))
    return w


async def reindex_all(w) -> None:
    builder = ContextBuilder()
    await builder.preload()
    res = await index.load_resources(builder.owner)
    await index.reset_cursors()
    while (await index.run_batch(index.BACKFILL, res, builder, 100)).last_id is not None:
        pass


async def test_text_words_are_applied_before_the_limit_not_to_the_newest_slice(world):
    gs, owner, lisa = world["gs"], world["owner"], world["lisa"]
    dm = {"chat_type": "user", "chat_id": "200", "sender_id": "200", "direction": "received"}
    await event(gs, 0, "telegram", "давно обсуждали HRM систему", day=1, meta=dm)      # единственное с темой
    for n in range(1, 260):
        await event(gs, n, "telegram", f"обычное сообщение номер {n}", day=2 + n % 20, meta=dm)
    await reindex_all(world)
    flt = EventFilter(participant_ids=(owner, lisa))
    newest, truncated = await filtered_events(flt, 200)
    assert truncated and not any("HRM" in e["content_preview"] for e in newest)    # так тема терялась
    found, _ = await filtered_events(flt, 200, ("hrm",))
    assert [e["content_preview"] for e in found] == ["давно обсуждали HRM систему"]
    assert (await filtered_events(flt, 200, ("hrm", "нет такого")))[0] == []


async def test_call_counterparts_resolve_by_surname_and_by_a_global_phrase_nickname(world):
    gs, director = world["gs"], world["director"]
    by_surname = await event(gs, 1, "voice", "созвон", meta={"counterparts": ["Собеседник 1", "Корчевский"]})
    by_phrase = await event(gs, 2, "voice", "Участники: Дмитрий Александрович", meta={"counterparts": ["Дмитрий Александрович", "Дмитрий"]})
    await reindex_all(world)
    async def participants(eid):
        async with gs() as s:
            rows = (await s.execute(select(EventEntityRow).where(
                EventEntityRow.event_id == eid, EventEntityRow.role == "participant"))).scalars().all()
        return {r.entity_id for r in rows}
    assert await participants(by_surname) == {director}
    assert await participants(by_phrase) == set()                  # фразы-прозвища ещё нет, «Дмитрий» неоднозначно
    await add_nickname(director, "Дмитрий Александрович", scope_kind="global")
    assert await index.reindex_token("Дмитрий Александрович") == 1
    assert await participants(by_phrase) == {director}


def test_surname_resolution_needs_a_unique_person_inside_the_circle():
    persons = [PersonNames(1, "Дмитрий Корчевский"), PersonNames(2, "Иван Корчевский"), PersonNames(3, "Олег Громов")]
    r = NameResolver(persons)
    assert r.surname("Громовым", {1, 3}) == 3 and r.surname("Громов", {1}) is None
    assert r.surname("Корчевский", {1, 2}) is None                 # двое — не гадаем
    assert r.surname("Корчевский", {1, 3}) == 1 and r.surname("Иван", {1, 2}) is None
    assert r.resolve("Корчевский", {1, 3}) == (1, "surname")


async def test_adding_a_nickname_through_the_tool_links_existing_events_at_once(world):
    from types import SimpleNamespace
    gs = world["gs"]
    work = {"chat_type": "chat", "chat_id": "-1005001", "sender_id": "200"}
    async with gs() as s:
        from sqlalchemy import text
        await s.execute(text("INSERT INTO project_membership (project, kind, key, source) "
                             "VALUES ('itstep', 'chat', '1005001', 'test')"))
    await event(gs, 1, "telegram", "ДА просил ознакомиться", meta=work)
    await event(gs, 2, "telegram", "да, давай созвонимся", meta=work)           # слово, не прозвище
    await reindex_all(world)
    async with gs() as s:
        assert not (await s.execute(select(EventEntityRow).where(
            EventEntityRow.entity_id == world["director"], EventEntityRow.role == "mentioned"))).first()
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={"mcp_client": "t"})))
    out = await lw.entity_add_nickname(world["director"], "ДА", ctx, scope="work")
    assert out["reindex_truncated"] is False and out["reindexed_events"] == 1                          # только событие с заглавным «ДА»
    async with gs() as s:
        rows = (await s.execute(select(EventEntityRow).where(
            EventEntityRow.entity_id == world["director"], EventEntityRow.role == "mentioned"))).scalars().all()
    assert [(r.source_of_link, r.scope_ok) for r in rows] == [("nickname", True)]


async def test_the_question_period_can_be_overridden_and_defaults_to_september(world):
    gs, lisa = world["gs"], world["lisa"]
    await event(gs, 1, "telegram", "в августе писала", meta={"chat_type": "user", "chat_id": "200", "sender_id": "200"})
    await reindex_all(world)
    case = next(c for c in CASES if c.id == "asked_the_team")
    binds = {"owner": world["owner"], "lisa": lisa, "director": world["director"], "oleg": 0, "topic": "x"}
    assert not (await run_case(case, binds)).answerable                         # по умолчанию сентябрь
    august = {**binds, "start": datetime(2026, 8, 1), "end": datetime(2026, 9, 1)}
    assert (await run_case(case, august)).answerable


async def test_reindex_source_rebuilds_only_that_sources_events(world):
    gs = world["gs"]
    call = await event(gs, 1, "voice", "созвон", meta={"counterparts": ["Корчевский"]})
    other = await event(gs, 2, "telegram", "привет", meta={"chat_type": "user", "chat_id": "200", "sender_id": "200"})
    assert await index.reindex_source("voice") == 1
    async with gs() as s:
        ids = {r.event_id for r in (await s.execute(select(EventEntityRow))).scalars()}
    assert call in ids and other not in ids


async def test_reindex_reports_truncation_and_refuses_while_the_cycle_lock_is_held(world, monkeypatch):
    from vera_shared.links import lock
    gs = world["gs"]
    meta = {"chat_type": "user", "chat_id": "200", "sender_id": "200"}
    for i in (1, 2, 3):
        await event(gs, i, "telegram", "ДА просил", meta=meta)
    done = await index.reindex_token_report("ДА", limit=2)
    assert (done.count, done.truncated) == (2, True)
    assert (await index.reindex_token_report("ДА", limit=3)).truncated is False

    @lock.asynccontextmanager
    async def busy():
        yield False
    monkeypatch.setattr(lock, "links_lock", busy)
    with pytest.raises(lock.LinksBusyError):
        await index.reindex_token("ДА")
    with pytest.raises(lock.LinksBusyError):
        await index.reindex_source("voice")

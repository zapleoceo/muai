"""Правки по ревью модуля связей: слияние переносит данные связей, ручные связи переживают
пересчёт, плохое событие не стопорит курсор, круг не зависит от порядка обработки, область
прозвища по проекту в личке, фамилия не по заглавной в начале предложения, ASR-отсев. Данные
синтетические."""
from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from vera_shared.db.models import EventRow
from vera_shared.db.models_links import (
    EntityNicknameRow,
    EventEntityRow,
    VoiceSpeakerMapRow,
)
from vera_shared.graph import merge, repo, unmerge
from vera_shared.links import asr, index, read
from vera_shared.links.context import ContextBuilder
from vera_shared.links.matcher import MentionMatcher, PersonNames
from vera_shared.links.nicknames import add_nickname
from vera_shared.links.scope import ChatContext, NicknameRule, in_scope
from vera_shared.links.speakers import put_speaker

pytestmark = pytest.mark.asyncio
OWNER_TG = "100"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER_TG)


async def person(name: str, tg: str) -> int:
    return await repo.upsert_entity(type="person", name=name, source="telegram", identifier=f"user:{tg}")


async def event(gs, n: int, source: str, body: str, **kw) -> int:
    async with gs() as s:
        row = EventRow(source=source, source_event_id=f"r{n}", content_text=body,
                       occurred_at=datetime(2026, 9, 1 + n % 25), triage_status="done",
                       metadata_=kw.pop("meta", {}), **kw)
        s.add(row)
        await s.flush()
        return row.id


@pytest_asyncio.fixture
async def world(sqlite_db):
    gs = sqlite_db
    w = {"gs": gs, "owner": await person("Dmitry Testov", OWNER_TG),
         "keep": await person("Виктор Кронов", "300"), "dup": await person("Витя Кронов", "301")}
    w["call"] = await event(gs, 1, "voice", "созвон", meta={"voices": ["Собеседник 2"]},
                            content_extra={"utterances": [{"speaker": "Собеседник 2", "text": "а"}]})
    w["other_call"] = await event(gs, 2, "voice", "созвон 2", meta={"voices": ["Собеседник 3"]},
                                  content_extra={"utterances": [{"speaker": "Собеседник 3", "text": "б"}]})
    w["chat"] = await event(gs, 3, "telegram", "привет", meta={
        "chat_type": "chat", "chat_id": "-1009", "sender_id": "301"})
    return w


async def rows_of(gs, model):
    async with gs() as s:
        return list((await s.execute(select(model))).scalars())


async def test_merge_moves_nicknames_voice_map_and_manual_links_and_unmerge_restores(world):
    gs, keep, dup = world["gs"], world["keep"], world["dup"]
    await add_nickname(dup, "ВП", scope_kind="work")
    await add_nickname(dup, "ККК", scope_kind="work")
    await add_nickname(keep, "ВП", scope_kind="global")          # у победителя уже есть — дубль уходит
    async with gs() as s:
        await put_speaker(s, world["call"], "Собеседник 2", dup)
        s.add(VoiceSpeakerMapRow(kind="voiceprint", key="vp-1", entity_id=dup))
    builder = ContextBuilder()
    await builder.preload()
    await index.run_batch(index.BACKFILL, await index.load_resources(builder.owner), builder, 50) \
        if False else None
    res = await index.load_resources(builder.owner)
    await index.reset_cursors()
    await index.run_batch(index.BACKFILL, res, builder, 50)
    before_derived = [r for r in await rows_of(gs, EventEntityRow) if r.entity_id == dup]
    assert any(r.role == "author" for r in before_derived)        # производная связь у дубля есть

    report = await merge.merge_entities(keep, [dup], "дубль")

    nicknames = {(r.entity_id, r.token, r.scope_kind) for r in await rows_of(gs, EntityNicknameRow)}
    assert nicknames == {(keep, "ВП", "global"), (keep, "ККК", "work")}
    assert {r.entity_id for r in await rows_of(gs, VoiceSpeakerMapRow)} == {keep}
    links = await rows_of(gs, EventEntityRow)
    assert not [r for r in links if r.entity_id == dup]
    manual = [r for r in links if r.source_of_link == "manual"]
    assert [(r.entity_id, r.event_id, r.token) for r in manual] == [(keep, world["call"], "Собеседник 2")]
    assert any(r.entity_id == keep and r.role == "author" and r.event_id == world["chat"] for r in links)
    assert report.counts()["event_entities_created"] == 1

    await unmerge.unmerge(report)
    nicknames = {(r.entity_id, r.token, r.scope_kind) for r in await rows_of(gs, EntityNicknameRow)}
    assert (dup, "ВП", "work") in nicknames and (dup, "ККК", "work") in nicknames
    assert {r.entity_id for r in await rows_of(gs, VoiceSpeakerMapRow)} == {dup}
    manual = [r for r in await rows_of(gs, EventEntityRow) if r.source_of_link == "manual"]
    assert [(r.entity_id, r.event_id) for r in manual] == [(dup, world["call"])]


async def test_a_manual_link_survives_a_rebuild_and_blocks_a_derived_duplicate_key(world):
    gs = world["gs"]
    async with gs() as s:
        await put_speaker(s, world["call"], "Собеседник 2", world["keep"])
    builder = ContextBuilder()
    await builder.preload()
    res = await index.load_resources(builder.owner)
    async with gs() as s:
        rows = list((await s.execute(select(EventRow))).scalars())
    for _ in range(2):
        await index.index_events(rows, res, builder)
    manual = [r for r in await rows_of(gs, EventEntityRow) if r.source_of_link == "manual"]
    assert len(manual) == 1 and manual[0].entity_id == world["keep"]


async def test_a_bad_event_is_skipped_and_the_cursor_moves_but_infrastructure_errors_propagate(world):
    gs = world["gs"]
    builder = ContextBuilder()
    await builder.preload()
    res = await index.load_resources(builder.owner)
    await index.reset_cursors()
    real = index.links_for

    def explode(view, *args, **kwargs):
        if view.id == world["other_call"]:
            raise ValueError("странный текст")
        return real(view, *args, **kwargs)

    with patch.object(index, "links_for", explode):
        result = await index.run_batch(index.BACKFILL, res, builder, 50)
    assert result.skipped == (world["other_call"],) and result.last_id == 1
    assert {r.event_id for r in await rows_of(gs, EventEntityRow)} >= {world["call"], world["chat"]}
    from sqlalchemy.exc import OperationalError

    def down(view, *args, **kwargs):
        raise OperationalError("select", {}, Exception("connection lost"))

    await index.reset_cursors()
    with patch.object(index, "links_for", down), pytest.raises(OperationalError):
        await index.run_batch(index.BACKFILL, res, builder, 50)


async def test_the_circle_does_not_depend_on_the_order_events_are_processed(sqlite_db):
    gs = sqlite_db
    owner = await person("Dmitry Testov", OWNER_TG)
    lisa = await person("Лиза Ветрова", "200")
    dima = await person("Олег Громов", "400")
    for n in range(3):          # более ранние сообщения Димы в группе; ниже — самое новое событие
        await event(gs, n, "telegram", "ок", meta={"chat_type": "chat", "chat_id": "-1009", "sender_id": "400"})
    late = await event(gs, 10, "telegram", "Олег, пришли файл", meta={
        "chat_type": "chat", "chat_id": "-1009", "sender_id": "200"})
    builder = ContextBuilder()
    await builder.preload()
    res = await index.load_resources(builder.owner)
    views = await index.load_views([late])             # обрабатываем ТОЛЬКО самое новое: backfill с конца
    await index.index_views(views, res, builder)
    rows = [r for r in await rows_of(gs, EventEntityRow) if r.event_id == late and r.role == "mentioned"]
    assert [(r.entity_id, r.confidence) for r in rows] == [(dima, 0.6)] and owner and lisa


async def test_view_loading_skips_heavy_columns_for_ordinary_events(world):
    views = {v.id: v for v in await index.load_views([world["call"], world["chat"]])}
    assert views[world["call"]].extra["utterances"] and views[world["chat"]].extra is None
    assert views[world["chat"]].transcript is None


async def test_hidden_events_have_no_participants_and_drop_their_links(world):
    gs = world["gs"]
    builder = ContextBuilder()
    await builder.preload()
    res = await index.load_resources(builder.owner)
    await index.reset_cursors()
    await index.run_batch(index.BACKFILL, res, builder, 50)
    assert await read.event_participants(world["chat"]) is not None
    async with gs() as s:
        (await s.get(EventRow, world["chat"])).triage_status = "hidden"
    assert await read.event_participants(world["chat"]) is None
    await index.reset_cursors()
    await index.run_batch(index.BACKFILL, res, builder, 50)
    assert not [r for r in await rows_of(gs, EventEntityRow) if r.event_id == world["chat"]]


def matcher(*persons: PersonNames) -> MentionMatcher:
    return MentionMatcher(list(persons), [])


def test_two_people_with_the_same_full_name_name_nobody():
    m = matcher(PersonNames(1, "Лиза Ветрова"), PersonNames(2, "Лиза Ветрова"), PersonNames(3, "Олег Громов"))
    assert m.find("Лиза Ветрова просила", ChatContext()) == []
    assert [x.entity_id for x in m.find("Олег Громов просил", ChatContext())] == [3]


def test_a_lone_surname_is_not_a_link_at_the_start_of_a_sentence_or_when_a_common_noun():
    m = matcher(PersonNames(1, "Иван Мельник"), PersonNames(2, "Игорь Кронов"))
    assert m.find("Мельник пришёл вовремя", ChatContext()) == []            # начало предложения
    assert m.find("Вчера. Мельник пришёл", ChatContext()) == []             # после точки
    assert m.find("Это Мельник, а не мельник на мельнице", ChatContext()) == []   # слово есть и строчным
    assert [x.entity_id for x in m.find("Это сказал Кронов вчера", ChatContext())] == [2]
    assert [x.entity_id for x in m.find("Мельник Иван здесь", ChatContext())] == [1]    # имя рядом


def test_project_scoped_nickname_in_a_dm_needs_the_partner_in_that_project():
    rule = NicknameRule(1, "ВП", True, "work", ("project:itstep",))
    strong = frozenset({7})
    inside = ChatContext(chat_key="telegram:7", dm_partner=7, dm_partner_projects=frozenset({"itstep"}))
    outside = ChatContext(chat_key="telegram:8", dm_partner=7, dm_partner_projects=frozenset({"veranda"}))
    chat_marked = ChatContext(chat_key="telegram:9", is_work=True, project="itstep", dm_partner=7)
    unknown = ChatContext(chat_key="telegram:10", dm_partner=7)
    assert in_scope(rule, inside, strong) and in_scope(rule, chat_marked, strong)
    assert not in_scope(rule, outside, strong) and not in_scope(rule, unknown, strong)
    plain = NicknameRule(1, "ВП", True, "work")
    assert in_scope(plain, unknown, strong)                 # без сужения личка с сильным контактом — рабочая


def test_asr_prefilter_keeps_the_match_and_is_fast_on_a_long_transcript():
    candidates = {i: f"Имя Фамилия{'абвгдежзик'[i % 10]}ов{i}" for i in range(150)}
    candidates[999] = "Виктор Корчагин"
    text = " ".join(f"Слово{i}ааааа Другое{i}бббббб" for i in range(1500)) + " Арчагин сказал"
    started = time.monotonic()
    found = asr.asr_matches(text, candidates)
    assert time.monotonic() - started < 5
    assert [(m.entity_id, m.heard) for m in found] == [(999, "Арчагин")]


async def test_asr_runs_off_the_event_loop():
    found = await asr.asr_matches_async("Арчагин сказал", {1: "Виктор Корчагин"})
    assert [m.entity_id for m in found] == [1]


async def test_owner_timeline_needs_a_period(world):
    from vera_mcp import read_tools as r
    with pytest.raises(ValueError, match="период"):
        await r.timeline(world["owner"])
    out = await r.timeline(world["owner"], start="2026-01-01", end="2027-01-01")
    assert "events" in out
    assert "events" in await r.timeline(world["keep"])

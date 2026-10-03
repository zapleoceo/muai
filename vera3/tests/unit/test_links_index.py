"""Индекс связей событий поверх SQLite: автор, получатель, участники созвона, упомянутые,
прозвища в области, чтение, фильтры, курсоры. Данные синтетические.

Сценарий-образец: у человека есть прозвище-инициалы, которое в рабочем чате значит его, а в
публичном — обычное слово; одиночное «Дима» в чате владельца — владелец, а не чужой тёзка.
"""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import PairStatsRow
from vera_shared.db.models_links import EventEntityRow
from vera_shared.graph import repo
from vera_shared.graph.pair_stats import ordered
from vera_shared.links import index, read
from vera_shared.links.context import ContextBuilder
from vera_shared.links.filters import EventFilter
from vera_shared.links.nicknames import add_nickname
from vera_shared.links.speakers import put_speaker

pytestmark = pytest.mark.asyncio
OWNER_TG = "100"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER_TG)


async def person(name: str, tg: str, email: str | None = None) -> int:
    eid = await repo.upsert_entity(type="person", name=name, source="telegram", identifier=f"user:{tg}")
    if email:
        await repo.upsert_entity_linked(type="person", name=name, source="gmail", identifier=email,
                                        known_as=[("telegram", f"user:{tg}")])
    return eid


async def event(get_session, n: int, source: str, text_: str, day: int = 1, **kw) -> int:
    async with get_session() as s:
        row = EventRow(source=source, source_event_id=f"e{n}", content_text=text_,
                       occurred_at=datetime(2026, 9, day, 10), triage_status=kw.pop("status", "done"),
                       metadata_=kw.pop("meta", {}), **kw)
        s.add(row)
        await s.flush()
        return row.id


@pytest_asyncio.fixture
async def world(sqlite_db):
    get_session = sqlite_db
    w = {"gs": get_session}
    w["owner"] = await person("Dmitry Testov", OWNER_TG, "owner@mine.example")
    w["lisa"] = await person("Лиза Ветрова", "200", "lisa@corp.example")
    w["director"] = await person("Виктор Корчагин", "300")
    w["namesake"] = await person("Дима", "999")
    async with get_session() as s:
        s.add(PairStatsRow(entity_a=ordered(w["owner"], w["director"])[0],
                           entity_b=ordered(w["owner"], w["director"])[1], active_days=20))
        await s.execute(text("INSERT INTO project_membership (project, kind, key, source) "
                             "VALUES ('itstep', 'chat', '1005001', 'test')"))
    await add_nickname(w["director"], "ВП", scope_kind="work")
    dm = {"chat_type": "user", "chat_id": "200"}
    w["dm_in"] = await event(get_session, 1, "telegram", "привет", meta={**dm, "sender_id": "200", "direction": "received"})
    w["dm_out"] = await event(get_session, 2, "telegram", "ответ", day=2, meta={**dm, "sender_id": OWNER_TG, "direction": "sent"})
    w["work"] = await event(get_session, 3, "telegram", "ВП просил ознакомиться с отчётом", day=3,
                            meta={"chat_type": "chat", "chat_id": "-1005001", "sender_id": "200"})
    w["public"] = await event(get_session, 4, "telegram", "ВП нужен для поездки", day=4,
                              meta={"chat_type": "chat", "chat_id": "-1009", "sender_id": "200"})
    w["mail"] = await event(get_session, 5, "gmail", "Subject: план\n---\nтекст", day=5,
                            meta={"from": "Лиза <lisa@corp.example>", "to": "owner@mine.example",
                                  "direction": "received"})
    w["dima"] = await event(get_session, 6, "telegram", "Дима, пришли файл", day=6,
                            meta={"chat_type": "chat", "chat_id": "-1005001", "sender_id": "200"})
    w["call"] = await event(
        get_session, 7, "voice", "Выжимка созвона", day=7,
        meta={"voices": ["Лиза Ветрова", "Собеседник 2"], "counterparts": [], "author_role": "self"},
        content_extra={"utterances": [{"speaker": "Лиза Ветрова", "text": "я"},
                                      {"speaker": "Собеседник 2", "text": "мы"}]},
        transcript_text="Арчагин просил подготовить отчёт")
    w["hidden"] = await event(get_session, 8, "telegram", "ВП скрыто", day=8, status="hidden",
                              meta={"chat_type": "chat", "chat_id": "-1005001", "sender_id": "200"})
    return w


async def build_all(w) -> int:
    builder = ContextBuilder()
    await builder.build([])
    res = await index.load_resources(builder.owner)
    async with w["gs"]() as s:
        rows = list((await s.execute(select(EventRow).order_by(EventRow.id))).scalars())
    return await index.index_events(rows, res, builder)


async def links_of(w, event_id: int) -> set[tuple[int, str, str]]:
    async with w["gs"]() as s:
        rows = (await s.execute(select(EventEntityRow).where(EventEntityRow.event_id == event_id))).scalars()
        return {(r.entity_id, r.role, r.source_of_link) for r in rows if r.scope_ok}


async def test_dm_has_author_and_recipient_in_both_directions(world):
    await build_all(world)
    o, lisa = world["owner"], world["lisa"]
    assert await links_of(world, world["dm_in"]) == {(lisa, "author", "alias"), (o, "recipient", "alias")}
    assert await links_of(world, world["dm_out"]) == {(o, "author", "alias"), (lisa, "recipient", "alias")}


async def test_mail_has_author_and_to_recipient(world):
    await build_all(world)
    assert await links_of(world, world["mail"]) == {
        (world["lisa"], "author", "alias"), (world["owner"], "recipient", "alias")}


async def test_nickname_counts_in_the_work_chat_and_is_kept_out_of_scope_in_the_public_one(world):
    await build_all(world)
    d = world["director"]
    assert (d, "mentioned", "nickname") in await links_of(world, world["work"])
    assert all(e != d for e, _, _ in await links_of(world, world["public"]))
    async with world["gs"]() as s:
        audit = (await s.execute(select(EventEntityRow).where(
            EventEntityRow.event_id == world["public"], EventEntityRow.entity_id == d))).scalar_one()
    assert audit.scope_ok is False and audit.token == "ВП"
    assert await read.mention_counts(d) == {"in_scope": 2, "out_of_scope": 1}   # +1: ASR в созвоне


async def test_first_name_in_the_owners_chat_is_the_owner_not_the_namesake(world):
    await build_all(world)
    linked = await links_of(world, world["dima"])
    assert (world["owner"], "mentioned", "name_match") in linked
    assert all(e != world["namesake"] for e, _, _ in linked)


async def test_hidden_events_get_no_links(world):
    await build_all(world)
    assert await links_of(world, world["hidden"]) == set()


async def test_call_participants_owner_named_voice_and_asr_guess(world):
    await build_all(world)
    linked = await links_of(world, world["call"])
    assert (world["owner"], "author", "alias") in linked
    assert (world["lisa"], "participant", "voiceprint") in linked
    assert (world["director"], "mentioned", "name_match") in linked      # «Арчагин» → Корчагин
    people = await read.event_participants(world["call"])
    assert [u["label"] for u in people["unresolved_speakers"]] == ["Собеседник 2"]


async def test_naming_an_unknown_voice_adds_the_participant_and_undoes(world):
    await build_all(world)
    async with world["gs"]() as s:
        assert await put_speaker(s, world["call"], "Собеседник 2", world["director"]) is None
    assert (world["director"], "participant", "manual") in await links_of(world, world["call"])
    people = await read.event_participants(world["call"])
    assert people["unresolved_speakers"] == [] or [u["label"] for u in people["unresolved_speakers"]] == []
    async with world["gs"]() as s:
        await put_speaker(s, world["call"], "Собеседник 2", None)
    assert (world["director"], "participant", "manual") not in await links_of(world, world["call"])
    await build_all(world)      # пересчёт карту голосов читает: после снятия связи нет
    assert (world["director"], "participant", "manual") not in await links_of(world, world["call"])


async def test_manual_speaker_survives_a_rebuild(world):
    async with world["gs"]() as s:
        await put_speaker(s, world["call"], "Собеседник 2", world["director"])
    await build_all(world)
    assert (world["director"], "participant", "manual") in await links_of(world, world["call"])


async def test_co_occurrence_and_filters(world):
    await build_all(world)
    o, lisa, d = world["owner"], world["lisa"], world["director"]
    both = await read.co_occurrence(o, lisa)
    assert {e["id"] for e in both["events"]} == {world["dm_in"], world["dm_out"], world["mail"],
                                                 world["work"], world["public"], world["dima"],
                                                 world["call"]} - {world["work"], world["public"],
                                                                   world["dima"]}
    assert both["by_kind"] == {"call": 1, "email": 1, "message": 2}
    calls, _ = await read.filtered_events(EventFilter(kind="call", with_owner=True))
    assert [e["id"] for e in calls] == [world["call"]]
    said, _ = await read.filtered_events(EventFilter(mentioned_ids=(d,)))
    assert {e["id"] for e in said} == {world["work"], world["call"]}
    mine, _ = await read.filtered_events(EventFilter(author_ids=(lisa,), kind="message"))
    assert {e["id"] for e in mine} == {world["dm_in"], world["work"], world["public"], world["dima"]}


async def test_mentions_and_timeline_roles(world):
    await build_all(world)
    d = world["director"]
    mentions = await read.mentioning_events(d)
    assert {m["id"] for m in mentions} == {world["work"], world["call"]}
    assert all(m["via"] in ("nickname", "name_match") for m in mentions)
    events = await read.entity_events(d, datetime(2026, 9, 1), datetime(2026, 10, 1), 10, ("mentioned",))
    assert {e["id"] for e in events} == {world["work"], world["call"]}
    assert events[0]["roles"] == ["mentioned"]


async def test_backfill_resumes_and_forward_picks_up_new_events(world):
    builder = ContextBuilder()
    await builder.build([])
    res = await index.load_resources(builder.owner)
    top = await index.reset_cursors()
    seen = 0
    while (result := await index.run_batch(index.BACKFILL, res, builder, 3)).last_id is not None:
        seen += result.events
    assert seen == top
    first = await links_of(world, world["work"])
    again = await index.run_batch(index.BACKFILL, res, builder, 3)
    assert again.last_id is None and await links_of(world, world["work"]) == first
    fresh = await event(world["gs"], 99, "telegram", "ВП снова", day=9,
                        meta={"chat_type": "chat", "chat_id": "-1005001", "sender_id": "200"})
    forward = await index.run_batch(index.FORWARD, res, builder, 10)
    assert forward.events == 1 and (world["director"], "mentioned", "nickname") in await links_of(world, fresh)
    assert (await index.run_batch(index.FORWARD, res, builder, 10)).last_id is None


async def test_first_name_and_patronymic_phrase_links_mail_and_calls_through_a_global_nickname(world):
    await add_nickname(world["director"], "Виктор Павлович", scope_kind="global")
    mail = await event(world["gs"], 40, "gmail", "Subject: план\n---\nСогласовано с Виктором Павловичем",
                       day=9, meta={"from": "Лиза <lisa@corp.example>", "to": "owner@mine.example"})
    await build_all(world)
    assert (world["director"], "mentioned", "nickname") in await links_of(world, mail)
    # в расшифровке звонка работает только область global; инициалы «ВП» (work) — нет
    call = await event(world["gs"], 41, "voice", "созвон", day=9, meta={}, transcript_text="ВП и Виктор Павлович")
    await build_all(world)
    tokens = {r for r in await links_of(world, call) if r[1] == "mentioned"}
    assert tokens == {(world["director"], "mentioned", "name_match")}

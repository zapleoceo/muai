"""12 вопросов владельца разной формы отвечаются цепочками общих инструментов на синтетическом
мире; считаем, сколько отвечалось до связей (разбор контракта) и сколько — после."""
from __future__ import annotations

from collections import Counter
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from vera_shared.db.models import EventRow
from vera_shared.graph import repo
from vera_shared.links import index
from vera_shared.links.context import ContextBuilder
from vera_shared.links.eval_questions import (
    CASES,
    NO,
    PARTIAL,
    YES,
    after_level,
    render_question,
    run_case,
)
from vera_shared.links.nicknames import add_nickname

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


@pytest_asyncio.fixture
async def world(sqlite_db):
    gs = sqlite_db
    w = {"owner": await person("Dmitry Testov", OWNER_TG, "owner@mine.example"),
         "lisa": await person("Лиза Ветрова", "200", "lisa@corp.example"),
         "director": await person("Виктор Кронов", "300", "boss@corp.example"),
         "oleg": await person("Олег Громов", "400", "oleg@corp.example"), "topic": "CRM"}
    async with gs() as s:
        await s.execute(text("INSERT INTO project_membership (project, kind, key, source) "
                             "VALUES ('itstep', 'chat', '1005001', 'test')"))
    await add_nickname(w["director"], "ВП", scope_kind="work")
    n = 0

    async def event(source: str, body: str, day: int, **kw) -> None:
        nonlocal n
        n += 1
        async with gs() as s:
            s.add(EventRow(source=source, source_event_id=f"ev{n}", content_text=body,
                           occurred_at=datetime(2026, 9, day, 10), triage_status="done", **kw))

    dm = {"chat_type": "user", "chat_id": "300"}
    await event("telegram", "отчёт готов", 2, metadata_={**dm, "sender_id": OWNER_TG, "direction": "sent"})
    await event("telegram", "жду отчёт до пятницы", 3, metadata_={**dm, "sender_id": "300", "direction": "received"})
    group = {"chat_type": "chat", "chat_id": "-1005001"}
    await event("telegram", "Команда, кто возьмёт интеграцию?", 4, metadata_={**group, "sender_id": "200"})
    await event("telegram", "ВП просил ознакомиться с регламентом", 5, metadata_={**group, "sender_id": "200"})
    await event("telegram", "Дима, пришли статус по проекту", 6, metadata_={**group, "sender_id": "200"})
    await event("telegram", "проект CRM ведёт Олег Громов", 7, metadata_={**group, "sender_id": "200"})
    await event("gmail", "Subject: план\n---\nпоручение", 8,
                metadata_={"from": "Лиза <lisa@corp.example>", "to": "boss@corp.example"})
    await event("gmail", "Subject: Alpha\n---\nпривет", 9,
                metadata_={"from": "Лиза <lisa@corp.example>", "to": "oleg@corp.example"})
    await event("telegram", "Прошу подготовить регламент CRM и отправить мне", 10,
                metadata_={**dm, "sender_id": "300", "direction": "received"})
    await event("voice", "Созвон про миграцию CRM", 11,
                metadata_={"voices": ["Виктор Кронов", "Собеседник 2"], "counterparts": []},
                content_extra={"utterances": [{"speaker": "Виктор Кронов", "text": "а"},
                                              {"speaker": "Собеседник 2", "text": "б"}]})
    builder = ContextBuilder()
    await builder.build([])
    async with gs() as s:
        rows = list((await s.execute(select(EventRow))).scalars())
    await index.index_events(rows, await index.load_resources(builder.owner), builder)
    return w


async def test_every_question_is_answered_by_generic_tools_after_the_links(world):
    results = [await run_case(case, world) for case in CASES]
    failed = [r.case.id for r in results if not r.answerable]
    assert failed == []
    levels = Counter(after_level(r) for r in results)
    assert levels == {YES: 11, PARTIAL: 1}                      # «Собеседник 2» называет владелец
    before = Counter(c.before for c in CASES)
    assert (before[YES], before[PARTIAL], before[NO]) == (2, 6, 4) and len(CASES) == 12


async def test_questions_render_with_names_and_chains_pass_the_event_between_steps(world):
    names = {"topic": "CRM", "lisa": "Лиза", "director": "Виктор", "oleg": "Олег"}
    assert render_question(CASES[0], names) == "Кто был на созвоне, где мы обсуждали CRM?"
    first = await run_case(CASES[0], world)
    assert [s["tool"] for s in first.steps] == ["filtered", "participants"] and first.answerable


async def test_a_question_nobody_can_answer_is_reported_as_unanswerable(world):
    unknown = {**world, "topic": "несуществующая тема"}
    assert not (await run_case(CASES[0], unknown)).answerable

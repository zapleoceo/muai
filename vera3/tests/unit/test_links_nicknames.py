"""Прозвища: запись и решение владельца, инициалы «Имя Отчество» из переписки, отчёт по области.
Данные синтетические."""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from vera_shared.db.models import EventRow
from vera_shared.graph import repo
from vera_shared.links import index
from vera_shared.links.context import ContextBuilder
from vera_shared.links.nickname_suggest import (
    initials,
    patronymic_forms,
    scope_report,
    suggest_initials,
)
from vera_shared.links.nicknames import (
    NicknameError,
    active_rules,
    add_nickname,
    decide_suggestion,
    pending_suggestions,
    suggest_nickname,
)
from vera_shared.links.scope import NicknameRule

pytestmark = pytest.mark.asyncio
OWNER_TG = "100"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER_TG)


async def person(name: str, tg: str) -> int:
    return await repo.upsert_entity(type="person", name=name, source="telegram", identifier=f"user:{tg}")


async def event(gs, n: int, body: str, chat: str, sender: str, kind: str = "chat", **extra) -> int:
    async with gs() as s:
        row = EventRow(source="telegram", source_event_id=f"k{n}", content_text=body,
                       occurred_at=datetime(2026, 9, n), triage_status="done",
                       metadata_={"chat_type": kind, "chat_id": chat, "sender_id": sender, **extra})
        s.add(row)
        await s.flush()
        return row.id


@pytest_asyncio.fixture
async def world(sqlite_db):
    gs = sqlite_db
    w = {"gs": gs, "owner": await person("Dmitry Testov", OWNER_TG),
         "director": await person("Виктор Корчагин", "300"), "lisa": await person("Лиза Ветрова", "200")}
    async with gs() as s:
        await s.execute(text("INSERT INTO project_membership (project, kind, key, source) "
                             "VALUES ('itstep', 'chat', '1005001', 'test')"))
    for n in (1, 2, 3):
        await event(gs, n, "Виктор Павлович, отчёт готов", "300", "100", "user", direction="sent")
    await event(gs, 4, "ВП просил ознакомиться", "-1005001", "200")
    await event(gs, 5, "ВП нужен в сервисе", "-1009", "200")
    await event(gs, 6, "вп строчными не считается", "-1005001", "200")
    await event(gs, 7, "KP asked", "-1005001", "200")
    await event(gs, 8, "kp asked", "-1005001", "200")
    builder = ContextBuilder()
    await builder.build([])
    async with gs() as s:
        rows = list((await s.execute(select(EventRow))).scalars())
    await index.index_events(rows, await index.load_resources(builder.owner), builder)
    return w


async def test_initials_and_patronymic_forms_are_found_in_addressing():
    assert initials("Виктор", "Павлович") == "ВП"
    forms = patronymic_forms(["Виктор Павлович, добрый день", "Виктору Павловичу — на подпись",
                              "Дмитрий Александрович не он"], "Виктор Корчагин")
    assert forms == {("Виктор", "Павлович"): 1}


async def test_suggestion_comes_from_the_addressing_and_waits_for_the_owner(world):
    assert await suggest_initials(world["director"], "Виктор Корчагин") == ["ВП"]
    assert await active_rules() == []               # само не применяется
    (pending,) = await pending_suggestions()
    assert pending["token"] == "ВП" and "3 раз" in pending["reason"]
    assert await suggest_initials(world["director"], "Виктор Корчагин") == []     # повтор не плодит
    assert await decide_suggestion(pending["id"], approve=True) is True
    (rule,) = await active_rules()
    assert (rule.token, rule.scope_kind, rule.case_sensitive) == ("ВП", "work", True)
    assert await decide_suggestion(pending["id"], approve=False) is False        # решение уже принято


async def test_rejected_suggestion_is_not_asked_again(world):
    await suggest_nickname(world["director"], "ВП", "причина")
    (pending,) = await pending_suggestions()
    await decide_suggestion(pending["id"], approve=False)
    assert await suggest_nickname(world["director"], "ВП", "ещё раз") is False
    assert await active_rules() == [] and await pending_suggestions() == []


async def test_add_validates_and_updates_in_place(world):
    with pytest.raises(NicknameError):
        await add_nickname(world["director"], "В")
    with pytest.raises(NicknameError):
        await add_nickname(world["director"], "ВП", scope_kind="everywhere")
    first = await add_nickname(world["director"], "ВП", scope_kind="work")
    second = await add_nickname(world["director"], "ВП", scope_kind="chats", scope_ids=["telegram:-1005001"])
    assert first == second
    (rule,) = await active_rules(world["director"])
    assert rule.scope_kind == "chats" and rule.scope_ids == ("telegram:-1005001",)


async def test_scope_report_splits_in_and_out_of_scope_and_ignores_case(world):
    report = await scope_report(NicknameRule(world["director"], "ВП", True, "work"))
    assert (report["in_scope"], report["out_of_scope"]) == (1, 1)
    assert report["top_in"] and report["top_out"]
    strict = await scope_report(NicknameRule(world["director"], "KP", True, "work"))
    loose = await scope_report(NicknameRule(world["director"], "KP", False, "work"))
    assert (strict["in_scope"], loose["in_scope"]) == (1, 2)

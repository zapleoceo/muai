"""«Последние события» карточки: все алиасы человека, скрытые не видны, без цитат. Синтетика."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import EntityAliasRow
from vera_shared.graph import panel, repo
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio


async def person(sqlite_db) -> int:
    a = await repo.upsert_entity(type="person", name="Иван Тестов", source="telegram",
                                 identifier="user:42")
    async with sqlite_db() as s:
        s.add(EntityAliasRow(entity_id=a, source="gmail", identifier="ivan@corp.example",
                             confidence=1.0))
    return a


async def event(sqlite_db, i: int, source: str, at: datetime, text: str, meta: dict,
                status: str = "done") -> None:
    async with sqlite_db() as s:
        s.add(EventRow(source=source, source_event_id=f"x{i}", content_text=text,
                       occurred_at=at, metadata_=meta, triage_status=status))


async def test_events_come_from_every_alias_newest_first_and_skip_hidden(sqlite_db):
    a = await person(sqlite_db)
    now = utc_naive_now()
    await event(sqlite_db, 1, "gmail", datetime(2026, 8, 14),
                "From: ivan@corp.example\nSubject: Старое\n---\nписьмо",
                {"from": "Ivan <ivan@corp.example>", "to": "me@x.example"})
    await event(sqlite_db, 2, "gmail", datetime(2026, 8, 20),
                "From: me@x.example\nSubject: Ответ\n---\nя ему",
                {"from": "me@x.example", "to": "ivan@corp.example"})
    await event(sqlite_db, 3, "telegram", now - timedelta(days=1), "---\nя в личку",
                {"sender_id": "169510539", "chat_id": "42"})
    await event(sqlite_db, 4, "telegram", now, "---\nон в личку", {"sender_id": "42", "chat_id": "42"})
    await event(sqlite_db, 5, "telegram", now, "---\nскрытое", {"sender_id": "42"}, status="hidden")
    await event(sqlite_db, 6, "telegram", now, "---\nчужой чат", {"sender_id": "7", "chat_id": "-100"})
    events = (await panel.entity_panel(a))["events"]
    assert [e["id"] for e in events] == [4, 3, 2, 1]
    assert events[2]["subject"] == "Ответ" and events[0]["subject"] == ""


async def test_email_snippet_drops_quoted_history(sqlite_db):
    a = await person(sqlite_db)
    body = ("Subject: Re: план\n---\nСогласен.\n\nOn Mon, Aug 11, 2026 at 10:00 AM Me "
            "<me@x.example> wrote:\n> старый текст")
    await event(sqlite_db, 1, "gmail", datetime(2026, 8, 14), body, {"from": "ivan@corp.example"})
    (found,) = (await panel.entity_panel(a))["events"]
    assert found["snippet"] == "Согласен." and found["subject"] == "Re: план"

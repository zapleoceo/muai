"""Скрытое событие (`hidden`) не читается ни одним потребителем содержимого."""
from __future__ import annotations

from datetime import datetime

import pytest
from vera_shared import chat_activity
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import EntityAliasRow, EntityRow
from vera_shared.events.visibility import NOT_HIDDEN_SQL, not_hidden_sql


def test_alias_qualifies_the_shared_predicate():
    assert not_hidden_sql() == NOT_HIDDEN_SQL
    assert not_hidden_sql("e") == "e.triage_status <> 'hidden'"


def test_dashboard_shows_hidden_in_russian():
    from dashboard.events_view import STATUS_LABEL, TRIAGE_STATUS_INFO

    icon, why = TRIAGE_STATUS_INFO["hidden"]
    assert icon and "скрыто" in why
    assert STATUS_LABEL["hidden"] == "скрыто"


@pytest.mark.asyncio
async def test_chat_activity_ignores_hidden_messages(sqlite_db):
    chat_activity.forget()
    async with sqlite_db() as s:
        for i, status in enumerate(["done", "done", "hidden"]):
            s.add(EventRow(source="telegram", source_event_id=f"m{i}", category="message",
                           content_text="hi", occurred_at=datetime(2026, 8, 27),
                           triage_status=status,
                           metadata_={"chat_id": 7, "direction": "sent"}))
    assert await chat_activity.own_message_count(7) == 2
    chat_activity.forget()


@pytest.mark.asyncio
async def test_dossier_samples_and_counts_skip_hidden_events(sqlite_db):
    from vera_shared.graph import dossiers

    async with sqlite_db() as s:
        ent = EntityRow(type="person", name="Alice", attributes={})
        s.add(ent)
        await s.flush()
        s.add(EntityAliasRow(entity_id=ent.id, source="telegram", identifier="user:42"))
        for i, status in enumerate(["done", "hidden", "hidden"]):
            s.add(EventRow(source="telegram", source_event_id=f"d{i}", category="message",
                           content_text=f"message {i}", occurred_at=datetime(2026, 1, 1 + i),
                           triage_status=status,
                           metadata_={"sender_id": "42", "chat_title": "Team"}))
        entity_id = ent.id
    got = (await dossiers.build([entity_id]))[entity_id]
    assert got["msg_count"] == 1
    assert [x for x in got["samples"] if "message 0" not in x] == []


@pytest.mark.asyncio
async def test_gmail_dossier_skips_hidden_events(sqlite_db):
    from vera_shared.graph import dossiers

    async with sqlite_db() as s:
        ent = EntityRow(type="person", name="Bob", attributes={})
        s.add(ent)
        await s.flush()
        s.add(EntityAliasRow(entity_id=ent.id, source="gmail", identifier="bob@example.com"))
        for i, status in enumerate(["done", "hidden"]):
            s.add(EventRow(source="gmail", source_event_id=f"g{i}", category="message",
                           content_text=f"mail {i}", occurred_at=datetime(2026, 1, 1 + i),
                           triage_status=status, metadata_={"from": "Bob <bob@example.com>"}))
        entity_id = ent.id
    got = (await dossiers.build([entity_id]))[entity_id]
    assert got["msg_count"] == 1

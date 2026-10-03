"""MCP: фильтры по людям в search / recent_events / timeline, event_participants, co_occurrence,
voice_speaker_set и entity_add_nickname (с откатом). Данные синтетические."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from vera_mcp import link_tools as lt
from vera_mcp import link_write_tools as lw
from vera_mcp import read_tools as r
from vera_mcp import write_tools as w
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_links import EntityNicknameRow, EventEntityRow
from vera_shared.db.models_mcp import McpAuditRow
from vera_shared.graph import repo
from vera_shared.links import index
from vera_shared.links.context import ContextBuilder
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
OWNER_TG = "100"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER_TG)


def ctx(client: str = "claude"):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


@pytest_asyncio.fixture
async def world(sqlite_db):
    async def person(name: str, tg: str) -> int:
        return await repo.upsert_entity(type="person", name=name, source="telegram",
                                        identifier=f"user:{tg}")

    w_ = {"owner": await person("Dmitry Testov", OWNER_TG), "lisa": await person("Лиза Ветрова", "200"),
          "director": await person("Виктор Корчагин", "300")}
    now = utc_naive_now()
    async with get_session() as s:
        dm = EventRow(source="telegram", source_event_id="d1", content_text="привет", occurred_at=now,
                      triage_status="done", metadata_={"chat_type": "user", "chat_id": "200",
                                                       "sender_id": "200", "direction": "received"})
        call = EventRow(source="voice", source_event_id="v1", content_text="Выжимка",
                        occurred_at=now, triage_status="done",
                        metadata_={"voices": ["Лиза Ветрова", "Собеседник 2"]},
                        content_extra={"utterances": [{"speaker": "Собеседник 2", "text": "мы"}]})
        s.add_all([dm, call])
        await s.flush()
        w_["dm"], w_["call"] = dm.id, call.id
    builder = ContextBuilder()
    await builder.build([])
    async with get_session() as s:
        rows = list((await s.execute(select(EventRow))).scalars())
    await index.index_events(rows, await index.load_resources(builder.owner), builder)
    return w_


async def test_search_forwards_the_people_filter_to_brain_search():
    fake = AsyncMock(return_value={"results": []})
    with patch.object(r, "search_brain", fake):
        await r.search("отчёт", 5, participant_ids=[1, 2], with_owner=True, kind="call",
                       start="2026-09-01")
    assert fake.await_args.kwargs["filters"] == {
        "participant_ids": [1, 2], "with_owner": True, "kind": "call", "start": "2026-09-01T00:00:00"}
    with patch.object(r, "search_brain", fake):
        await r.search("без фильтра", 5)
    assert "filters" not in fake.await_args.kwargs


async def test_bad_filter_is_a_value_error_not_an_empty_answer():
    with pytest.raises(ValueError):
        lt.link_filter(kind="video")
    with pytest.raises(ValueError):
        lt.link_filter(participant_ids=list(range(11)))


async def test_event_participants_lists_people_and_unresolved_voices(world):
    out = await lt.event_participants(world["call"])
    assert [p["name"] for p in out["participants"]] == ["Dmitry Testov", "Лиза Ветрова"]
    assert [u["label"] for u in out["unresolved_speakers"]] == ["Собеседник 2"]
    with pytest.raises(LookupError):
        await lt.event_participants(9999)


async def test_co_occurrence_counts_by_kind(world):
    out = await lt.co_occurrence(world["owner"], world["lisa"])
    assert out["by_kind"] == {"call": 1, "email": 0, "message": 1} and out["count"] == 2


async def test_recent_events_and_timeline_use_the_people_filter(world):
    calls = await r.recent_events(hours=24, kind="call", with_owner=True)
    assert [e["id"] for e in calls["events"]] == [world["call"]]
    mine = await r.recent_events(hours=24, participant_ids=[world["lisa"]], kind="message")
    assert [e["id"] for e in mine["events"]] == [world["dm"]]
    tl = await r.timeline(world["lisa"], roles=["author"])
    assert [e["id"] for e in tl["events"]] == [world["dm"]] and tl["events"][0]["roles"] == ["author"]


async def test_naming_a_voice_is_audited_and_undone(world):
    out = await lw.voice_speaker_set(world["call"], "Собеседник 2", world["director"], ctx())
    assert out["ok"] and out["previous_entity_id"] is None
    people = await lt.event_participants(world["call"])
    assert world["director"] in [p["entity_id"] for p in people["participants"]]
    assert people["unresolved_speakers"] == []
    await w.undo(out["audit_id"], ctx())
    assert world["director"] not in [p["entity_id"] for p in (await lt.event_participants(world["call"]))["participants"]]
    with pytest.raises(ValueError):
        await lw.voice_speaker_set(world["dm"], "Лиза", world["lisa"], ctx())      # не созвон


async def test_nickname_tool_is_audited_validated_and_undone(world):
    out = await lw.entity_add_nickname(world["director"], "ВП", ctx(), scope="work")
    async with get_session() as s:
        row = (await s.execute(select(EntityNicknameRow))).scalar_one()
        audit = (await s.execute(select(McpAuditRow).where(McpAuditRow.id == out["audit_id"]))).scalar_one()
    assert (row.token, row.status, row.scope_kind) == ("ВП", "active", "work")
    assert (audit.tool, audit.target_kind, audit.before) == ("entity_add_nickname", "nickname", None)
    with pytest.raises(ValueError):
        await lw.entity_add_nickname(world["director"], "В", ctx())
    await w.undo(out["audit_id"], ctx())
    async with get_session() as s:
        assert (await s.execute(select(EntityNicknameRow))).first() is None
        assert (await s.execute(select(EventEntityRow).where(EventEntityRow.role == "mentioned"))).first() is None


async def test_nickname_update_is_restored_on_undo(world):
    await lw.entity_add_nickname(world["director"], "ВП", ctx(), scope="work")
    out = await lw.entity_add_nickname(world["director"], "ВП", ctx(), scope="global")
    await w.undo(out["audit_id"], ctx())
    async with get_session() as s:
        assert (await s.execute(select(EntityNicknameRow.scope_kind))).scalar_one() == "work"


"""Метки сессий: имена агентов берутся из конфигурации, а не только из истории."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from vera_mcp import room_handoff_tools as h
from vera_mcp import room_tools as r
from vera_mcp.room_oauth import configured_agents
from vera_shared.room import messages

TOKEN = "t" * 40


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE, CODEX = ctx("claude"), ctx("codex")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ROOM_NAMES", "main")
    monkeypatch.setenv("ROOM_TOKENS", f"claude:{TOKEN},codex:{TOKEN}y")
    monkeypatch.setenv("MCP_TOKENS", f"owner:{TOKEN}z")
    monkeypatch.setenv("ROOM_OAUTH_ACTORS", "dot")


def test_configured_agents_unions_all_sources(monkeypatch):
    assert configured_agents() == {"claude", "codex", "owner", "dot", "vera", "watchdog"}


def test_configured_agents_tolerates_empty_env(monkeypatch):
    for key in ("ROOM_TOKENS", "MCP_TOKENS", "MCP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ROOM_OAUTH_ACTORS", " , ")
    assert configured_agents() == {"vera", "watchdog"}


@pytest.mark.asyncio
async def test_never_posted_agent_name_is_rejected_as_label(sqlite_db):
    for bad in ("dot", "vera", "codex"):
        with pytest.raises(ValueError, match="reserved"):
            await r.room_post("hi", CLAUDE, session=bad)
        with pytest.raises(ValueError, match="reserved"):
            await r.room_task_claim("t1", CLAUDE, session=bad)
        with pytest.raises(ValueError, match="reserved"):
            await r.room_task_next(CLAUDE, session=bad)
        with pytest.raises(ValueError, match="reserved"):
            await h.room_task_handoff_accept("t1", CLAUDE, session=bad)


@pytest.mark.asyncio
async def test_dispatcher_prefix_is_reserved_for_vera(sqlite_db):
    with pytest.raises(ValueError, match="reserved"):
        await r.room_post("hi", CLAUDE, session="dispatcher:abc")
    await r.room_post("hi", ctx("vera"), session="dispatcher:abc")


@pytest.mark.asyncio
async def test_legit_custom_and_own_name_labels_still_work(sqlite_db):
    res = await r.room_post("hi", CLAUDE, session="laptop")
    assert res["message"]["session"] == "laptop"
    assert (await r.room_post("me", CLAUDE, session="claude"))["ok"]


@pytest.mark.asyncio
async def test_historical_squat_of_never_posted_name_gets_no_mail(sqlite_db):
    await messages.post_message(room="main", message_id="old", from_agent="claude",
                                body="squat", from_session="dot")
    await r.room_post("to dot", CODEX, to="dot")
    await r.room_post("to laptop", CODEX, to="laptop")
    await messages.post_message(room="main", message_id="old2", from_agent="claude",
                                body="legit", from_session="laptop")
    got = [m["body"] for m in (await r.room_inbox(CLAUDE))["messages"]]
    assert "to dot" not in got and "to laptop" in got

"""room_inbox_wait: отвечает сразу при непрочитанном, будится room_post, без дублей по after_id."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from vera_mcp import room_inbox_wait as w
from vera_mcp import room_tools as r
from vera_mcp import room_wake

pytestmark = pytest.mark.asyncio


def _ctx(client: str) -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOM_NAMES", "main")
    monkeypatch.setattr(r.room_push, "schedule_push", lambda _m: None)


async def test_returns_at_once_when_unread_exists(sqlite_db) -> None:
    await r.room_post("ping", _ctx("dot"), to="claude", message_id="m1")
    started = time.monotonic()
    res = await w.room_inbox_wait(_ctx("claude"), timeout_s=5)
    assert [m["body"] for m in res["messages"]] == ["ping"]
    assert res["timed_out"] is False
    assert time.monotonic() - started < 2


async def test_times_out_without_messages(sqlite_db) -> None:
    res = await w.room_inbox_wait(_ctx("claude"), timeout_s=1)
    assert res["messages"] == [] and res["timed_out"] is True


async def test_wakes_on_post_during_wait(sqlite_db) -> None:
    waiter = asyncio.create_task(w.room_inbox_wait(_ctx("claude"), timeout_s=20))
    await asyncio.sleep(0.2)
    started = time.monotonic()
    await r.room_post("hello", _ctx("dot"), to="claude", message_id="m2")
    res = await asyncio.wait_for(waiter, 5)
    assert [m["body"] for m in res["messages"]] == ["hello"]
    assert time.monotonic() - started < 2


async def test_own_message_does_not_wake(sqlite_db, monkeypatch) -> None:
    monkeypatch.setattr(w, "RECHECK_S", 0.2)
    waiter = asyncio.create_task(w.room_inbox_wait(_ctx("claude"), timeout_s=1))
    await asyncio.sleep(0.1)
    await r.room_post("self", _ctx("claude"), to="dot", message_id="own")
    res = await waiter
    assert res["timed_out"] is True


async def test_after_id_skips_already_seen(sqlite_db) -> None:
    first = await r.room_post("one", _ctx("dot"), to="claude", message_id="a1")
    seen = first["message"]["id"]
    timed = await w.room_inbox_wait(_ctx("claude"), timeout_s=1, after_id=seen)
    assert timed["timed_out"] is True
    await r.room_post("two", _ctx("dot"), to="claude", message_id="a2")
    res = await w.room_inbox_wait(_ctx("claude"), timeout_s=1, after_id=seen)
    assert [m["body"] for m in res["messages"]] == ["two"]


async def test_recheck_catches_message_without_signal(sqlite_db, monkeypatch) -> None:
    monkeypatch.setattr(w, "RECHECK_S", 0.2)

    async def silent_bump() -> None:
        return None

    monkeypatch.setattr(r.room_wake, "bump", silent_bump)
    waiter = asyncio.create_task(w.room_inbox_wait(_ctx("claude"), timeout_s=5))
    await asyncio.sleep(0.1)
    await r.room_post("quiet", _ctx("dot"), to="claude", message_id="q1")
    res = await asyncio.wait_for(waiter, 3)
    assert [m["body"] for m in res["messages"]] == ["quiet"]


async def test_wait_since_sees_bump_and_times_out(sqlite_db) -> None:
    seen = room_wake.version()
    assert await room_wake.wait_since(seen, 0.1) is False
    asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(room_wake.bump()))
    assert await room_wake.wait_since(seen, 2) is True

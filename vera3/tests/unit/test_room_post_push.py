"""room_post передаёт новое сообщение в push один раз, повтор message_id — не передаёт."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from vera_mcp import room_tools as r

pytestmark = pytest.mark.asyncio


def _ctx(client: str) -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


@pytest.fixture(autouse=True)
def _rooms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOM_NAMES", "main")


@pytest.fixture
def pushed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(r.room_push, "schedule_push", calls.append)
    return calls


async def test_new_message_is_handed_to_push(sqlite_db, pushed) -> None:
    res = await r.room_post("ping", _ctx("dot"), to="claude", message_id="p1")
    assert [m["id"] for m in pushed] == [res["message"]["id"]]
    assert (pushed[0]["from"], pushed[0]["to"], pushed[0]["body"]) == ("dot", "claude", "ping")


async def test_deduped_repeat_is_not_pushed_again(sqlite_db, pushed) -> None:
    await r.room_post("ping", _ctx("dot"), to="claude", message_id="p1")
    again = await r.room_post("ping", _ctx("dot"), to="claude", message_id="p1")
    assert again["deduped"] is True
    assert len(pushed) == 1

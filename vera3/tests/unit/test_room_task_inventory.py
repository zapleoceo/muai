"""Keyset inventory must enumerate the registry without starting work."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from vera_mcp import room_tools as r

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _rooms(monkeypatch):
    monkeypatch.setenv("ROOM_NAMES", "main,other")


def _ctx():
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": "dot"})))


async def test_inventory_complete_and_legacy_list_unchanged(sqlite_db):
    for task_id in ("a", "b", "c", "d", "e"):
        await r.room_task_open(task_id, _ctx())
    assert len((await r.room_tasks(limit=2))["tasks"]) == 2
    cursor = None
    collected = []
    for _ in range(3):
        page = await r.room_task_inventory(limit=2, cursor=cursor)
        collected.extend(t["task_id"] for t in page["tasks"])
        assert page["expected_count"] == 5
        cursor = page["next_cursor"]
        if page["complete"]:
            assert page["returned_count"] == 5
            assert page["inconsistent"] is False
            break
        assert cursor is not None
    assert collected == ["a", "b", "c", "d", "e"]


async def test_inventory_detects_concurrent_insert_inside_bound(sqlite_db):
    for task_id in ("a", "c", "e"):
        await r.room_task_open(task_id, _ctx())
    first = await r.room_task_inventory(limit=1)
    await r.room_task_open("d", _ctx())
    second = await r.room_task_inventory(limit=3, cursor=first["next_cursor"])
    assert [t["task_id"] for t in second["tasks"]] == ["c", "d", "e"]
    assert second["expected_count"] == 3
    assert second["returned_count"] == 4
    assert second["inconsistent"] is True
    assert second["complete"] is False
    assert second["next_cursor"] is None


async def test_inventory_empty_and_cross_room_cursor_rejected(sqlite_db):
    assert (await r.room_task_inventory())["complete"] is True
    for task_id in ("a", "b"):
        await r.room_task_open(task_id, _ctx())
    cursor = (await r.room_task_inventory(limit=1))["next_cursor"]
    with pytest.raises(ValueError, match="invalid inventory cursor"):
        await r.room_task_inventory(room="other", cursor=cursor)
    with pytest.raises(ValueError, match="invalid inventory cursor"):
        await r.room_task_inventory(cursor="not-base64")

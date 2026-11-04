"""The Room writer must reject malformed Office snapshots before persistence."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vera_mcp import room_tools

UUID = "01a12abb-b3b4-74dc-aeeb-3432e9fd509f"


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": "dot"})))


@pytest.mark.asyncio
async def test_invalid_entry_does_not_reach_room_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = AsyncMock()
    monkeypatch.setattr(room_tools.messages, "post_message", writer)
    monkeypatch.setattr(room_tools.messages, "already_posted", AsyncMock(return_value=False))
    body = "OFFICE_RUNTIME_V1\n" + json.dumps({
        "checked_at": datetime.now(UTC).isoformat(),
        "entries": [{"project": "P", "state": "in_progress", "detail": "d",
                     "source": "codex"}],
    })
    with pytest.raises(ValueError, match=r"entries\[0\]\.state"):
        await room_tools.room_post(body, _ctx(), to="office-dashboard")
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_entry_reaches_room_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = AsyncMock(return_value=({"message_id": "office-valid"}, False))
    monkeypatch.setattr(room_tools.messages, "post_message", writer)
    monkeypatch.setattr(room_tools.messages, "already_posted", AsyncMock(return_value=False))
    monkeypatch.setattr(room_tools, "message_view", lambda row: row)
    checked_at = datetime.now(UTC).isoformat()
    body = "OFFICE_RUNTIME_V1\n" + json.dumps({
        "checked_at": checked_at,
        "entries": [{"project": "P", "state": "active", "detail": "d",
                     "source": "codex", "thread_id": UUID}],
        "sources": {"codex": {"coverage": "partial", "observed_at": checked_at}},
    })
    result = await room_tools.room_post(body, _ctx(), to="office-dashboard")
    assert result["ok"] is True
    writer.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_of_existing_snapshot_keeps_room_idempotency(
        monkeypatch: pytest.MonkeyPatch) -> None:
    writer = AsyncMock(return_value=({"message_id": "office-old"}, True))
    monkeypatch.setattr(room_tools.messages, "post_message", writer)
    monkeypatch.setattr(room_tools.messages, "already_posted", AsyncMock(return_value=True))
    monkeypatch.setattr(room_tools, "message_view", lambda row: row)
    old = "OFFICE_RUNTIME_V1\n" + json.dumps({"checked_at": "2026-01-01T00:00:00Z",
                                                "entries": []})
    result = await room_tools.room_post(old, _ctx(), to="office-dashboard",
                                        message_id="office-old")
    assert result["deduped"] is True
    writer.assert_awaited_once()

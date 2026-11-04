"""The Room writer must reject malformed Office snapshots before persistence."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from vera_mcp import room_tools
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomMessageRow
from vera_shared.office.runtime_snapshot import (
    parse_runtime_snapshot,
    validate_runtime_publication,
)
from vera_shared.room.messages import MessageConflict

UUID = "01a12abb-b3b4-74dc-aeeb-3432e9fd509f"


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": "dot"})))


def _body(checked_at: datetime, detail: str = "d") -> str:
    return "OFFICE_RUNTIME_V1\n" + json.dumps({
        "checked_at": checked_at.isoformat(),
        "entries": [{"project": "P", "state": "active", "detail": detail,
                     "source": "codex", "thread_id": UUID}],
    })


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


def test_publication_rechecks_actual_receipt_boundary() -> None:
    now = datetime(2026, 10, 14, 12, tzinfo=UTC)
    body = _body(now - timedelta(hours=2))
    validate_runtime_publication(body, now)
    with pytest.raises(ValueError, match="timestamp contract"):
        validate_runtime_publication(body, now + timedelta(microseconds=1),
                                     created_at=now + timedelta(microseconds=1))


@pytest.mark.asyncio
async def test_database_receipt_rejection_rolls_back_post(sqlite_db) -> None:
    async with get_session() as session:
        await session.execute(text("""
            CREATE TRIGGER office_future_receipt AFTER INSERT ON room_messages
            BEGIN
                UPDATE room_messages SET created_at = datetime('now', '+3 seconds')
                WHERE id = NEW.id;
            END
        """))
    body = _body(datetime.now(UTC) - timedelta(hours=2) + timedelta(seconds=1))
    with pytest.raises(ValueError, match="timestamp contract"):
        await room_tools.room_post(body, _ctx(), to="office-dashboard",
                                   message_id="boundary-post")
    assert (await room_tools.room_history())["messages"] == []


@pytest.mark.asyncio
async def test_storage_idempotency_conflict_and_actor_attribution(sqlite_db) -> None:
    # SQLite CURRENT_TIMESTAMP has second precision, unlike PostgreSQL.
    body = _body(datetime.now(UTC) - timedelta(seconds=1))
    first = await room_tools.room_post(body, _ctx(), to="office-dashboard",
                                       message_id="runtime-same")
    again = await room_tools.room_post(body, _ctx(), to="office-dashboard",
                                       message_id="runtime-same")
    assert again["deduped"] is True
    assert again["message"]["id"] == first["message"]["id"]
    with pytest.raises(MessageConflict, match="different payload"):
        await room_tools.room_post(_body(datetime.now(UTC), "changed"), _ctx(),
                                   to="office-dashboard", message_id="runtime-same")
    other = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": "claude"})))
    with pytest.raises(MessageConflict, match="already used by dot"):
        await room_tools.room_post(body, other, to="office-dashboard",
                                   message_id="runtime-same")
    foreign = await room_tools.room_post(body, other, to="office-dashboard",
                                         message_id="runtime-foreign")
    async with get_session() as session:
        stored = await session.get(RoomMessageRow, foreign["message"]["id"])
        assert stored is not None and stored.from_agent == "claude"
        assert parse_runtime_snapshot(stored, datetime.now(UTC)) is None

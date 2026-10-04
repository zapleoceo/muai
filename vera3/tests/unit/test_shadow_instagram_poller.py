"""The feature flag preserves existing polling and enables retry delivery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SAME_CONTENT = (
    "Author: Я [self]\nFrom: @owner-a\nChat: Test\n"
    "Date: 2026-10-03T00:00:00+00:00\nDirection: sent\n---\nhello"
)
SAME_METADATA = {
    "thread_id": "thread-1",
    "thread_title": "Test",
    "is_group": False,
    "sender_id": 5,
    "sender_username": "owner-a",
    "direction": "sent",
    "author_role": "self",
    "author_label": "Я",
    "message_id": "message-2",
    "item_type": None,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,sync_mode,expected",
    [
        (False, "missing", 0),
        (True, "missing", 1),
        (True, "same", 0),
        (True, "old", 1),
        (True, "meta", 1),
    ],
)
async def test_seen_message_retries_only_if_shadow_missing_or_edited(
    monkeypatch, enabled, sync_mode, expected
):
    poller = import_module("ingestor_instagram.__main__")
    if enabled:
        monkeypatch.setenv("VERA_SHADOW_INSTAGRAM_ENABLED", "1")
    else:
        monkeypatch.delenv("VERA_SHADOW_INSTAGRAM_ENABLED", raising=False)
    message = SimpleNamespace(
        id="message-2",
        user_id=5,
        text="hello",
        timestamp=datetime(2026, 10, 3, tzinfo=UTC),
    )
    thread = SimpleNamespace(id="thread-1", users=[], thread_title="Test")
    client = SimpleNamespace(
        user_id=5,
        direct_threads=lambda **_: [thread],
        direct_messages=lambda *_args, **_kwargs: [message],
    )
    sent = AsyncMock()
    monkeypatch.setattr(
        poller, "_existing_sids", AsyncMock(return_value={"ig:thread-1:message-2"})
    )
    metadata = SAME_METADATA if sync_mode != "meta" else {"thread_id": "old"}
    synced = (
        {}
        if sync_mode == "missing"
        else {
            "ig:thread-1:message-2": (
                "digest" if sync_mode != "old" else "old",
                SAME_CONTENT,
                metadata,
            )
        }
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr(poller, "get_session", fake_session)
    monkeypatch.setattr(poller, "instagram_event_hash", lambda _event: "digest")
    monkeypatch.setattr(
        poller, "synced_instagram_state", AsyncMock(return_value=synced)
    )
    monkeypatch.setattr(poller, "post_event", sent)
    assert await poller.poll_once(client, "owner-a") == expected
    assert sent.await_count == expected


@pytest.mark.asyncio
async def test_existing_sids_returns_a_set_of_identifiers(monkeypatch):
    poller = import_module("ingestor_instagram.__main__")

    class Result:
        def scalars(self):
            return self

        def all(self):
            return ["ig:thread-1:message-2"]

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def fake_session():
        yield Session()

    monkeypatch.setattr(poller, "get_session", fake_session)
    assert await poller._existing_sids(["ig:thread-1:message-2"]) == {
        "ig:thread-1:message-2"
    }

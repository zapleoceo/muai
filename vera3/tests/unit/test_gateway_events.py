"""gateway.events — GET /api/events/{event_id} auth (previously had NONE at
all, unlike every other route in this module) + shared auth helper."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from gateway.events import get_event


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _row_session(row):
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    return session


@pytest.mark.asyncio
async def test_get_event_rejects_missing_secret():
    with pytest.raises(HTTPException) as exc:
        await get_event(1, x_internal_secret=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_event_rejects_wrong_secret():
    with pytest.raises(HTTPException) as exc:
        await get_event(1, x_internal_secret="wrong")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_event_returns_row_with_correct_secret():
    row = SimpleNamespace(
        id=1, source="gmail", source_event_id="msg1", account="a@b.com",
        category="email", content_text="hi", occurred_at=datetime(2026, 1, 1),
        received_at=datetime(2026, 1, 1), triage_status="done",
        triage_metadata=None, importance=50, nature="world_event",
        project="itstep", ready_subtype=None,
    )
    session = _row_session(row)
    with patch("gateway.events.get_session",
               MagicMock(return_value=_FakeSessionCtx(session))):
        result = await get_event(1, x_internal_secret="test-internal-secret")
    assert result["id"] == 1
    assert result["content_text"] == "hi"


@pytest.mark.asyncio
async def test_get_event_404_when_not_found():
    session = _row_session(None)
    with patch("gateway.events.get_session",
               MagicMock(return_value=_FakeSessionCtx(session))), \
         pytest.raises(HTTPException) as exc:
        await get_event(1, x_internal_secret="test-internal-secret")
    assert exc.value.status_code == 404

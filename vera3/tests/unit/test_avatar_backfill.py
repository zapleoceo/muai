"""ingestor_telegram.avatar_backfill._fetch_one — the per-entity fetch/store
branches. The forever-loop is thin orchestration around this."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ingestor_telegram.avatar_backfill import _fetch_one
from telethon.errors import FloodWaitError


def _client(photo_return=None, exc=None):
    c = MagicMock()
    c.download_profile_photo = AsyncMock(
        return_value=photo_return, side_effect=exc)
    return c


@pytest.mark.asyncio
async def test_stores_downloaded_photo():
    client = _client(photo_return=b"\xff\xd8jpeg")
    with patch("ingestor_telegram.avatar_backfill.upsert_avatar",
               AsyncMock()) as up:
        await _fetch_one(client, {"id": 5, "tg_id": 42, "username": "u"})
    up.assert_awaited_once()
    kwargs = up.await_args.kwargs
    assert kwargs["image"] == b"\xff\xd8jpeg"
    assert up.await_args.args[0] == 5


@pytest.mark.asyncio
async def test_marks_missing_when_no_photo():
    client = _client(photo_return=None)
    with patch("ingestor_telegram.avatar_backfill.upsert_avatar",
               AsyncMock()) as up:
        await _fetch_one(client, {"id": 6, "tg_id": 43, "username": None})
    assert up.await_args.kwargs["missing"] is True
    assert up.await_args.kwargs["image"] is None


@pytest.mark.asyncio
async def test_marks_missing_on_resolve_error():
    client = _client(exc=ValueError("Could not find the input entity"))
    with patch("ingestor_telegram.avatar_backfill.upsert_avatar",
               AsyncMock()) as up:
        await _fetch_one(client, {"id": 7, "tg_id": 44, "username": None})
    assert up.await_args.kwargs["missing"] is True


@pytest.mark.asyncio
async def test_marks_missing_when_no_identifier():
    client = _client()
    with patch("ingestor_telegram.avatar_backfill.upsert_avatar",
               AsyncMock()) as up:
        await _fetch_one(client, {"id": 8, "tg_id": None, "username": None})
    client.download_profile_photo.assert_not_called()
    assert up.await_args.kwargs["missing"] is True


@pytest.mark.asyncio
async def test_floodwait_propagates():
    fw = FloodWaitError.__new__(FloodWaitError)
    fw.seconds = 30
    client = _client(exc=fw)
    with patch("ingestor_telegram.avatar_backfill.upsert_avatar", AsyncMock()), \
         pytest.raises(FloodWaitError):
        await _fetch_one(client, {"id": 9, "tg_id": 45, "username": None})

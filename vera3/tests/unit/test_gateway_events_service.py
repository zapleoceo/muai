"""gateway: приём события вынесен из маршрута — сервис собирает строку и
различает новое событие и дедуп, маршрут только проверяет секрет и source."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from gateway.events import ingest_event
from gateway.events_service import get_event_view, ingest_raw_event
from vera_shared.events.schema import RawEvent

SECRET = "test-internal-secret"


def _event(**over) -> RawEvent:
    base = {"source": "gmail", "source_event_id": "m1", "account": "a@b.c",
            "category": "email", "content_text": "hi",
            "occurred_at": datetime(2026, 1, 1)}
    return RawEvent(**{**base, **over})


@pytest.mark.asyncio
async def test_new_event_is_inserted_as_pending():
    insert = AsyncMock(return_value=(42, False))
    with patch("gateway.events_service.insert_event_once", insert):
        out = await ingest_raw_event(_event())
    assert out == {"ok": True, "event_id": 42, "deduped": False}
    values = insert.await_args.args[0]
    assert values["triage_status"] == "pending"
    assert (values["source"], values["source_event_id"]) == ("gmail", "m1")


@pytest.mark.asyncio
async def test_duplicate_returns_existing_id():
    with patch("gateway.events_service.insert_event_once",
               AsyncMock(return_value=(7, True))):
        out = await ingest_raw_event(_event())
    assert out == {"ok": True, "event_id": 7, "deduped": True}


@pytest.mark.asyncio
async def test_route_rejects_source_mismatch_before_touching_db():
    insert = AsyncMock()
    with patch("gateway.events_service.insert_event_once", insert), \
         pytest.raises(HTTPException) as exc:
        await ingest_event("telegram", _event(), x_internal_secret=SECRET)
    assert exc.value.status_code == 400
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_rejects_bad_secret():
    with pytest.raises(HTTPException) as exc:
        await ingest_event("gmail", _event(), x_internal_secret="wrong")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_route_source_check_is_case_insensitive():
    with patch("gateway.events_service.insert_event_once",
               AsyncMock(return_value=(1, False))):
        out = await ingest_event("GMAIL", _event(), x_internal_secret=SECRET)
    assert out["event_id"] == 1


@pytest.mark.asyncio
async def test_missing_event_view_is_none():
    with patch("gateway.events_service.get_event_row", AsyncMock(return_value=None)):
        assert await get_event_view(5) is None

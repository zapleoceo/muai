"""dashboard «Входящее»: фильтр, страница и карточка события живут в
`events_service`, SQL — в `events_repo`. Здесь — поведение без HTTP."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from dashboard.events_service import events_filter, load_event_card, load_events_page


def test_no_filters_means_no_where():
    assert events_filter(None, None, "", "") == ("", {})


def test_user_text_only_reaches_params():
    where, params = events_filter("gmail", "done", "50%_off", "")
    assert where == ("WHERE e.source = :source AND e.triage_status = :status "
                     "AND e.content_text ILIKE :q ESCAPE '\\'")
    assert "50" not in where
    assert params == {"source": "gmail", "status": "done", "q": "%50\\%\\_off%"}


def test_cursor_becomes_keyset_condition():
    where, params = events_filter(None, None, "", "2026-10-03T10:00:00_7")
    assert "(e.occurred_at, e.id) < (:before_at, :before_id)" in where
    assert params == {"before_at": datetime(2026, 10, 3, 10), "before_id": 7}


@pytest.mark.asyncio
async def test_page_asks_one_extra_row_to_know_there_is_more():
    fetch = AsyncMock(return_value=[{"id": i} for i in range(4)])
    with patch("dashboard.events_service.fetch_event_page", fetch):
        page = await load_events_page(3, None, None, "x", "")
    assert fetch.await_args.args[1]["limit"] == 4
    assert fetch.await_args.kwargs["bounded"] is True
    assert page.has_more and len(page.rows) == 3 and not page.timed_out


@pytest.mark.asyncio
async def test_timeout_is_reported_not_raised():
    with patch("dashboard.events_service.fetch_event_page", AsyncMock(return_value=None)):
        page = await load_events_page(3, None, None, "x", "")
    assert page.timed_out and page.rows == [] and not page.has_more


@pytest.mark.asyncio
async def test_missing_card_is_none():
    with patch("dashboard.events_service.get_event_card_row", AsyncMock(return_value=None)):
        assert await load_event_card(1) is None


@pytest.mark.asyncio
async def test_card_decodes_sqlite_json_metadata():
    row = {"id": 1, "source": "telegram", "content_text": "hi",
           "metadata": '{"sender_id": 5}'}
    with patch("dashboard.events_service.get_event_card_row", AsyncMock(return_value=row)), \
         patch("dashboard.events_service.find_entity_by_alias", AsyncMock(return_value=None)):
        card = await load_event_card(1)
    assert card is not None
    got, links = card
    assert got["metadata"] == {"sender_id": 5} and links == {}

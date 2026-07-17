"""Gmail backlog: id-only list + дедуп до GET + курсор стоит при truncated.

Сценарий бага: бэклог >MAX_PER_RUN — старый код забирал 500 НОВЕЙШИХ,
двигал last_polled_at на сегодня, и старый хвост терялся навсегда."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-gmail", "src"))

os.environ.setdefault("GMAIL_CLIENT_ID", "test-cid")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "test-csec")

from ingestor_gmail import poller  # noqa: E402
from vera_shared.db.engine import Base  # noqa: E402
from vera_shared.db.models import EventRow  # noqa: E402


class _FakeResp:
    def __init__(self, json_data):
        self.status_code = 200
        self.text = ""
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


class _PagedClient:
    """Отдаёт страницы list API по pageToken."""

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append(params or {})
        idx = int((params or {}).get("pageToken") or 0)
        return _FakeResp(self._pages[idx])


@pytest.mark.asyncio
async def test_fetch_message_ids_walks_pages_and_caps():
    pages = [
        {"messages": [{"id": f"m{i}"} for i in range(100)], "nextPageToken": "1"},
        {"messages": [{"id": f"m{100 + i}"} for i in range(100)], "nextPageToken": "2"},
        {"messages": [{"id": f"m{200 + i}"} for i in range(50)]},
    ]
    client = _PagedClient(pages)
    with patch.object(poller.httpx, "AsyncClient", lambda timeout: client):
        ids = await poller.fetch_message_ids("tok", "after:2026/07/01", cap=1000)
        assert len(ids) == 250 and ids[0] == "m0" and ids[-1] == "m249"

        capped = await poller.fetch_message_ids("tok", "q", cap=120)
        assert len(capped) == 120  # предохранитель, страницы дальше не листаются


@pytest_asyncio.fixture
async def db(tmp_path):
    import vera_shared.db.engine as engine_mod
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None
    from vera_shared.db.engine import get_session, init_engine
    engine = await init_engine(f"sqlite+aiosqlite:///{tmp_path / 'g.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield get_session
    await engine.dispose()
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None


async def _seed(get_session, ids: list[str]):
    from datetime import datetime
    async with get_session() as s:
        for i in ids:
            s.add(EventRow(source="gmail", source_event_id=i, account="a@b.c",
                           category="email", content_text="x",
                           occurred_at=datetime(2026, 7, 1),
                           triage_status="done"))


@pytest.mark.asyncio
async def test_filter_new_ids_skips_known_before_expensive_get(db):
    await _seed(db, ["m1", "m2"])
    new_ids, truncated = await poller.filter_new_ids(["m1", "m2", "m3", "m4"], per_run=10)
    assert new_ids == ["m3", "m4"]
    assert truncated is False


@pytest.mark.asyncio
async def test_filter_new_ids_flags_truncation_when_backlog_exceeds_per_run(db):
    new_ids, truncated = await poller.filter_new_ids([f"m{i}" for i in range(7)], per_run=5)
    assert len(new_ids) == 7          # режет вызывающий код, не фильтр
    assert truncated is True


@pytest.mark.asyncio
async def test_filter_new_ids_empty(db):
    assert await poller.filter_new_ids([], per_run=5) == ([], False)

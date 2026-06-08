"""Тесты базового интерфейса SourceConnector."""
from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator

import pytest

from vera_shared.connectors.base import (
    ConnectorCapability,
    SourceConnector,
)
from vera_shared.events.schema import RawEvent


class StubConnector(SourceConnector):
    """Минимальная реализация для тестов framework."""

    name = "stub"
    capabilities = {ConnectorCapability.BACKFILL}

    async def authenticate(self, credentials):
        self._authenticated = True

    async def fetch_history(self, start, end, **kwargs):
        for i in range(3):
            yield RawEvent(
                source=self.name,
                source_event_id=f"stub-{i}",
                occurred_at=start,
                content_text=f"Event {i}",
            )


@pytest.mark.asyncio
class TestConnectorBase:
    async def test_authenticate_sets_flag(self):
        c = StubConnector()
        await c.authenticate({})
        health = await c.health_check()
        assert health["authenticated"] is True

    async def test_fetch_history_yields_events(self):
        c = StubConnector()
        events = []
        async for e in c.fetch_history(datetime(2026, 1, 1), datetime(2026, 6, 1)):
            events.append(e)
        assert len(events) == 3
        assert all(e.source == "stub" for e in events)

    async def test_fetch_history_yields_valid_raw_events(self):
        c = StubConnector()
        async for e in c.fetch_history(datetime(2026, 1, 1), datetime(2026, 6, 1)):
            assert isinstance(e, RawEvent)
            assert e.source_event_id.startswith("stub-")

    async def test_health_check_reports_capabilities(self):
        c = StubConnector()
        h = await c.health_check()
        assert "backfill" in h["capabilities"]
        assert h["name"] == "stub"

    async def test_subscribe_realtime_not_supported_raises(self):
        # StubConnector не имеет REALTIME → должен поднять NotImplementedError
        c = StubConnector()
        with pytest.raises(NotImplementedError):
            await c.subscribe_realtime(lambda e: None)  # type: ignore[arg-type, return-value]

    async def test_parse_bulk_archive_not_supported_raises(self):
        c = StubConnector()
        with pytest.raises(NotImplementedError):
            async for _ in c.parse_bulk_archive("/tmp/x.zip"):
                pass

    async def test_account_propagated(self):
        c = StubConnector(account="alice@example.com")
        assert c.account == "alice@example.com"
        h = await c.health_check()
        assert h["account"] == "alice@example.com"

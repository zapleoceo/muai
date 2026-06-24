"""vera_shared.sources.base — ABCs + dataclasses."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vera_shared.sources.base import (
    DirectoryDelta,
    EventEnvelope,
    Source,
)


def test_event_envelope_minimal():
    e = EventEnvelope(
        source="gmail",
        source_event_id="msg_42",
        occurred_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        content_text="hello",
    )
    assert e.source == "gmail"
    assert e.account is None
    assert e.category is None
    assert e.attachments == []
    assert e.entity_hints == []
    assert e.metadata == {}


def test_event_envelope_with_optional_fields():
    e = EventEnvelope(
        source="telegram",
        source_event_id="42",
        occurred_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        content_text="x",
        account="zaplo",
        category="dm",
        attachments=[{"type": "photo", "url": "x"}],
        entity_hints=[{"name": "Маша"}],
        metadata={"chat_id": 123},
    )
    assert e.account == "zaplo"
    assert e.metadata["chat_id"] == 123


def test_directory_delta_defaults_zero():
    d = DirectoryDelta()
    assert d.entities_upserted == 0
    assert d.aliases_upserted == 0
    assert d.memberships_upserted == 0
    assert d.memberships_deactivated == 0
    assert d.relationships_upserted == 0


def test_directory_delta_with_counts():
    d = DirectoryDelta(entities_upserted=5, aliases_upserted=3)
    assert d.entities_upserted == 5
    assert d.aliases_upserted == 3


def test_source_is_abstract():
    """Can't instantiate Source directly — poll + backfill are abstract."""
    with pytest.raises(TypeError):
        Source()  # type: ignore[abstract]


async def test_source_default_sync_directory_returns_empty_delta():
    """The default sync_directory is a no-op returning an empty DirectoryDelta."""
    class StubSource(Source):
        name = "stub"
        async def poll(self):  # type: ignore[override]
            return
            yield  # unreachable, makes this a generator
        async def backfill(self, since):  # type: ignore[override]
            return
            yield

    delta = await StubSource().sync_directory()
    assert isinstance(delta, DirectoryDelta)
    assert delta.entities_upserted == 0


def test_source_default_tools_returns_empty_list():
    class StubSource(Source):
        name = "stub"
        async def poll(self):  # type: ignore[override]
            return
            yield
        async def backfill(self, since):  # type: ignore[override]
            return
            yield

    assert StubSource().tools() == []

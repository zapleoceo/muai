"""Source ABC — standard contract for every input source.

Adding a new source = implement this + register. Source-agnostic core.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, AsyncIterator

from vera_shared.tools.base import Tool


@dataclass
class EventEnvelope:
    """Normalized event passed to the gateway. Source-agnostic."""
    source: str
    source_event_id: str
    occurred_at: datetime
    content_text: str
    account: str | None = None
    category: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    entity_hints: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DirectoryDelta:
    """What sync_directory() returns. Counts of upserts."""
    entities_upserted: int = 0
    aliases_upserted: int = 0
    memberships_upserted: int = 0
    memberships_deactivated: int = 0
    relationships_upserted: int = 0


class Source(ABC):
    """Mandatory: poll + backfill. Optional: sync_directory + tools."""

    name: str  # 'gmail' | 'telegram' | 'instagram' | …

    @abstractmethod
    def poll(self) -> AsyncIterator[EventEnvelope]:
        """Yield events newer than last_polled_at."""

    @abstractmethod
    def backfill(self, since: date) -> AsyncIterator[EventEnvelope]:
        """Yield events from `since` to now, oldest first."""

    async def sync_directory(self) -> DirectoryDelta:
        """Upsert Entity/Membership/Relationship rows for this source.
        Default: no-op. Override per source."""
        return DirectoryDelta()

    def tools(self) -> list[Tool]:
        """Live tools the agent loop can call. Default: []."""
        return []

"""Standard contract for every input source (gmail, telegram, bank, …).

Adding a new source = subclass `Source`, implement `poll()` + `backfill()`,
register in `app.sources.registry`. The brain (ingest, decide, backfill jobs)
treats all sources identically — no per-source branching in the core.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(slots=True)
class Attachment:
    kind: str                              # 'image' | 'pdf' | 'audio' | 'other'
    sha256: str
    ocr_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EntityHint:
    type: str                              # 'person' | 'project' | 'account' | …
    identifier: str                        # canonical key (email, @handle, id)
    name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EventEnvelope:
    """Normalised event shape — every source yields this."""
    source: str
    source_event_id: str                   # stable, for dedup
    occurred_at: datetime
    content_text: str
    account: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    entity_hints: list[EntityHint] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class Source(ABC):
    """All event sources implement these two methods. Nothing more."""

    name: str                              # unique, matches sources.name in DB
    type: str                              # gmail|telegram|bank|instagram|…

    @abstractmethod
    def poll(self) -> AsyncIterator[EventEnvelope]:
        """Yield events newer than the source's last_polled_at."""

    @abstractmethod
    def backfill(self, since: date) -> AsyncIterator[EventEnvelope]:
        """Yield events from `since` to now, oldest first. Resumable."""

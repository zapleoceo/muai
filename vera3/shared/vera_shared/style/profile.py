"""StyleProfile — what Vera knows about how Dima writes to a person/group.

Stored as IdentityNodeRow(type='style', listener_entity_id=…).payload.
Profile is observed (not configured) — see brain-voice for builder.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Formality = Literal["vy", "ty", "mixed", "unknown"]


@dataclass
class SampleMessage:
    event_id: int
    text: str
    occurred_at: str  # ISO


@dataclass
class StyleProfile:
    """One per (Dima → listener). Global one has listener_entity_id=None."""
    speaker: str = "dima"
    listener_entity_id: int | None = None
    listener_label: str = "unknown"
    based_on_n_messages: int = 0

    formality: Formality = "unknown"
    avg_length_chars: int = 0
    avg_sentences: float = 0.0
    emoji_per_msg: float = 0.0
    frequent_emoji: list[str] = field(default_factory=list)
    openings: list[str] = field(default_factory=list)
    closings: list[str] = field(default_factory=list)
    vocabulary_signatures: list[str] = field(default_factory=list)
    code_switching: dict[str, float] = field(default_factory=dict)
    median_response_latency_min: float | None = None
    sample_messages: list[SampleMessage] = field(default_factory=list)
    updated_at: str = ""
    confidence: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_payload(cls, p: dict[str, Any]) -> "StyleProfile":
        samples = [SampleMessage(**m) for m in p.get("sample_messages", [])]
        return cls(**{**p, "sample_messages": samples})

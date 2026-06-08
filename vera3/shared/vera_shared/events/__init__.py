"""Event schema — каноническое представление любого события."""
from vera_shared.events.schema import (
    EntityHint,
    RawEvent,
    Signal,
    SignalType,
    TriageMetadata,
)

__all__ = [
    "RawEvent",
    "EntityHint",
    "Signal",
    "SignalType",
    "TriageMetadata",
]

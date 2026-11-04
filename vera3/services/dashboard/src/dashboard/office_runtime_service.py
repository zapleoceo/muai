"""Bounded, attributed runtime observations posted through existing Room OAuth."""
from __future__ import annotations

from vera_shared.office.runtime_snapshot import (  # noqa: F401
    CODEX,
    COVERAGES,
    MAX_AGE,
    PREFIX,
    RECIPIENT,
    UNKNOWN_SOURCE,
    RuntimeEntry,
    RuntimeSnapshot,
    latest_runtime_snapshot,
    parse_coverage,
    parse_entry,
    parse_runtime_snapshot,
    snapshot_error,
    utc_naive,
)

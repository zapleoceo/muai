"""Bounded, attributed runtime observations posted through existing Room OAuth."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from vera_shared.db.models_room import RoomMessageRow

PREFIX = "OFFICE_RUNTIME_V1\n"
RECIPIENT = "office-dashboard"
MAX_AGE = timedelta(hours=1)
_THREAD = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_STATES = {"active", "completed", "waiting", "unknown"}
COVERAGES = ("partial", "complete", "unavailable")
CODEX = "codex"
_SOURCE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
UNKNOWN_SOURCE = "unknown"


def utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


@dataclass(frozen=True)
class RuntimeEntry:
    project: str
    thread_id: str
    state: str
    detail: str
    source: str = CODEX
    source_id: str = ""


@dataclass(frozen=True)
class RuntimeSnapshot:
    checked_at: datetime
    received_at: datetime
    message_id: str
    entries: tuple[RuntimeEntry, ...]
    coverage: tuple[tuple[str, str, datetime | None], ...] = ()

    def source_coverage(self, source: str) -> tuple[str, datetime | None]:
        """Declared coverage and observation time; undeclared (legacy V1) is unknown: its
        entries are historical only and never enter current counters."""
        for name, coverage, observed in self.coverage:
            if name == source:
                return coverage, observed
        return "unknown", None

    def declared(self, source: str) -> bool:
        return any(name == source for name, _, _ in self.coverage)

    def is_fresh(self, now: datetime) -> bool:
        return timedelta(0) <= utc_naive(now) - self.checked_at <= MAX_AGE


def parse_entry(item: dict) -> RuntimeEntry | None:
    """V1 entry: no `source` means codex with a UUID thread_id. Other sources keep their
    native `source_id` as given; nothing is invented for them."""
    project, state, detail = item["project"], item["state"], item["detail"]
    source = item.get("source", CODEX)
    if (not all(isinstance(v, str) for v in (project, state, detail, source)) or
            not 1 <= len(project) <= 64 or state not in _STATES or len(detail) > 1000 or
            not _SOURCE.fullmatch(source)):
        return None
    if source == CODEX:
        thread_id = item["thread_id"]
        if not isinstance(thread_id, str) or not _THREAD.fullmatch(thread_id):
            return None
        return RuntimeEntry(project, thread_id, state, detail, source, thread_id)
    source_id = item.get("source_id", "")
    if (not isinstance(source_id, str) or len(source_id) > 128 or
            (not source_id and source != UNKNOWN_SOURCE)):
        return None
    return RuntimeEntry(project, "", state, detail, source, source_id)


def parse_coverage(raw: object, checked: datetime, now: datetime
                   ) -> tuple[tuple[str, str, datetime | None], ...] | None:
    if raw is None:
        return ()
    if not isinstance(raw, dict) or len(raw) > 20:
        return None
    out = []
    for name, spec in raw.items():
        if (not isinstance(spec, dict) or not _SOURCE.fullmatch(name) or
                spec.get("coverage") not in COVERAGES):
            return None
        observed = None
        if "observed_at" in spec:
            observed = datetime.fromisoformat(str(spec["observed_at"]).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                return None
            observed = utc_naive(observed)
            if observed > checked or observed > utc_naive(now):
                return None
        out.append((name, spec["coverage"], observed))
    return tuple(out)


def parse_runtime_snapshot(row: RoomMessageRow, now: datetime) -> RuntimeSnapshot | None:
    """Accept only a recent dot observation addressed to the dashboard."""
    if (row.room != "main" or row.from_agent != "dot" or row.to_agent != RECIPIENT or
            row.task_id is not None or row.status != "info" or
            not row.body.startswith(PREFIX) or len(row.body) > 12000):
        return None
    try:
        raw = json.loads(row.body[len(PREFIX):])
        checked = datetime.fromisoformat(raw["checked_at"].replace("Z", "+00:00"))
        if checked.tzinfo is None or not isinstance(raw["entries"], list):
            return None
        checked = checked.astimezone(UTC).replace(tzinfo=None)
        received = utc_naive(row.created_at)
        if (not 0 <= (received - checked).total_seconds() <= 7200 or
                checked > utc_naive(now) or len(raw["entries"]) > 20):
            return None
        entries = []
        seen = set()
        for item in raw["entries"]:
            entry = parse_entry(item)
            if entry is None:
                return None
            key = (entry.source, entry.source_id)
            if entry.source_id and key in seen:
                return None
            seen.add(key)
            entries.append(entry)
        coverage = parse_coverage(raw.get("sources"), checked, now)
        if coverage is None:
            return None
        return RuntimeSnapshot(checked, received, row.message_id, tuple(entries), coverage)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def latest_runtime_snapshot(rows: list[RoomMessageRow], now: datetime) -> RuntimeSnapshot | None:
    """A newer malformed observation must not silently resurrect an older count."""
    ranked = []
    for row in rows:
        snapshot = parse_runtime_snapshot(row, now)
        if snapshot is not None:
            checked = snapshot.checked_at
        else:
            try:
                raw = json.loads(row.body[len(PREFIX):])
                checked = datetime.fromisoformat(raw["checked_at"].replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = utc_naive(row.created_at)
                else:
                    checked = utc_naive(checked)
            except (KeyError, TypeError, ValueError, AttributeError):
                checked = utc_naive(row.created_at)
        ranked.append((checked, utc_naive(row.created_at), row.message_id, snapshot))
    return max(ranked, default=(None, None, None, None))[3]


def snapshot_error(row: RoomMessageRow, now: datetime) -> str | None:
    """Return a field path, never untrusted message content, for rejected snapshots."""
    if parse_runtime_snapshot(row, now) is not None:
        return None
    if (row.room != "main" or row.from_agent != "dot" or
            row.to_agent != RECIPIENT or row.task_id is not None or row.status != "info"):
        return "address or attributed author"
    if not row.body.startswith(PREFIX):
        return "OFFICE_RUNTIME_V1 prefix"
    if len(row.body) > 12000:
        return "body exceeds 12000 characters"
    try:
        raw = json.loads(row.body[len(PREFIX):])
        if not isinstance(raw, dict):
            return "JSON object"
        entries = raw.get("entries")
        if not isinstance(entries, list) or len(entries) > 20:
            return "entries must be a list of at most 20"
        for index, item in enumerate(entries):
            field = f"entries[{index}]"
            if not isinstance(item, dict):
                return f"{field} must be an object"
            if item.get("state") not in _STATES:
                return f"{field}.state must be active, completed, waiting or unknown"
            if not isinstance(item.get("detail"), str) or len(item["detail"]) > 1000:
                return f"{field}.detail must be at most 1000 characters"
            if item.get("source", CODEX) == CODEX and not _THREAD.fullmatch(
                    str(item.get("thread_id", ""))):
                return f"{field}.thread_id requires a real Codex UUID"
            if parse_entry(item) is None:
                return f"{field} violates the runtime entry contract"
        sources = raw.get("sources")
        if isinstance(sources, dict):
            for name, spec in sources.items():
                if not isinstance(name, str) or not _SOURCE.fullmatch(name):
                    return "sources key must be a valid source name"
                if not isinstance(spec, dict) or spec.get("coverage") not in COVERAGES:
                    return f"sources.{name}.coverage must be partial, complete or unavailable"
        return "checked_at, source coverage, duplicate ID or timestamp contract"
    except (TypeError, ValueError, KeyError, AttributeError):
        return "invalid JSON or timestamp"


def validate_runtime_publication(body: str, now: datetime) -> None:
    """Fail a malformed addressed runtime post before Room acknowledges persistence."""
    row = RoomMessageRow(room="main", from_agent="dot", to_agent=RECIPIENT,
                         task_id=None, status="info", body=body, created_at=now,
                         message_id="preflight")
    if (reason := snapshot_error(row, now)) is not None:
        raise ValueError(f"OFFICE_RUNTIME_V1 rejected: {reason}")

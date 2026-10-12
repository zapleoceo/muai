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


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


@dataclass(frozen=True)
class RuntimeEntry:
    project: str
    thread_id: str
    state: str
    detail: str


@dataclass(frozen=True)
class RuntimeSnapshot:
    checked_at: datetime
    received_at: datetime
    message_id: str
    entries: tuple[RuntimeEntry, ...]

    def is_fresh(self, now: datetime) -> bool:
        return timedelta(0) <= _utc_naive(now) - self.checked_at <= MAX_AGE


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
        received = _utc_naive(row.created_at)
        if (not 0 <= (received - checked).total_seconds() <= 7200 or
                checked > _utc_naive(now) or len(raw["entries"]) > 20):
            return None
        entries = []
        seen = set()
        for item in raw["entries"]:
            project, thread_id, state, detail = (
                item["project"], item["thread_id"], item["state"], item["detail"])
            if (not all(isinstance(v, str) for v in (project, thread_id, state, detail)) or
                    not 1 <= len(project) <= 64 or not _THREAD.fullmatch(thread_id) or
                    state not in _STATES or len(detail) > 500 or thread_id in seen):
                return None
            seen.add(thread_id)
            entries.append(RuntimeEntry(project, thread_id, state, detail))
        return RuntimeSnapshot(checked, received, row.message_id, tuple(entries))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def latest_runtime_snapshot(rows: list[RoomMessageRow], now: datetime) -> RuntimeSnapshot | None:
    """Prefer the newest actual check even if an older observation arrives late."""
    valid = (snapshot for row in rows
             if (snapshot := parse_runtime_snapshot(row, now)) is not None)
    return max(valid, key=lambda s: (s.checked_at, s.received_at, s.message_id), default=None)

"""Разбор scope-записи dot (`OFFICE_SCOPE_V1`): отмены владельца, исключённый
бэклог Jira и текущие передачи. Запись — только данные отчёта, не смена статуса."""
from __future__ import annotations

import json
from typing import Any

from vera_shared.db.models_room import RoomMessageRow

SCOPE_PREFIX = "OFFICE_SCOPE_V1\n"


def parse_scope_decisions(row: RoomMessageRow) -> tuple[
    dict[tuple[str, str], str], dict[tuple[str, str], str], dict[tuple[str, str], str]
] | None:
    """Read a dot-attributed scope record, never treating it as a Room status change."""
    if (row.room != "main" or row.from_agent != "dot" or
            row.to_agent != "office-dashboard" or row.task_id is not None or
            row.status != "info" or not row.body.startswith(SCOPE_PREFIX) or
            len(row.body) > 12000):
        return None
    try:
        raw: Any = json.loads(row.body[len(SCOPE_PREFIX):])
        parsed = []
        for name in ("pending_owner_cancellations", "excluded_jira_backlog",
                     "current_handoffs"):
            entries = raw[name]
            if not isinstance(entries, list) or len(entries) > 100:
                return None
            out: dict[tuple[str, str], str] = {}
            for entry in entries:
                room, task_id, source = entry["room"], entry["task_id"], entry["source"]
                if (room != "main" or not isinstance(task_id, str) or
                        not 1 <= len(task_id) <= 128 or not isinstance(source, str) or
                        not source.startswith("room:") or not source[5:].isdigit() or
                        (room, task_id) in out):
                    return None
                out[(room, task_id)] = source
            parsed.append(out)
        if (parsed[0].keys() & parsed[1].keys() or
                parsed[0].keys() & parsed[2].keys() or
                parsed[1].keys() & parsed[2].keys()):
            return None
        return parsed[0], parsed[1], parsed[2]
    except (KeyError, TypeError, ValueError):
        return None


def _latest_scope_record(rows: list[RoomMessageRow]) -> tuple[RoomMessageRow, tuple] | None:
    """A bad newest record means unknown coverage, never an older silent fallback."""
    if not rows:
        return None
    parsed = parse_scope_decisions(rows[0])
    return (rows[0], parsed) if parsed is not None else None

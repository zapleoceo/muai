"""Представление строки задачи комнаты для MCP и тестов."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from vera_shared.db.models_room import RoomTaskRow
from vera_shared.timeutil import utc_naive_now


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def task_dict(r: RoomTaskRow) -> dict[str, Any]:
    now = utc_naive_now()
    live = bool(r.lease_holder and r.lease_until and r.lease_until > now)
    return {"task_id": r.task_id, "room": r.room, "title": r.title, "status": r.status,
            "created_by": r.created_by, "lease_holder": r.lease_holder if live else None,
            "lease_until": r.lease_until.isoformat() if live and r.lease_until else None,
            "fencing_token": r.fencing_token, "paths": list(r.paths or []), "note": r.note,
            "updated_at": r.updated_at.isoformat(), "priority": r.priority, "owner": r.owner,
            "next_action": r.next_action, "refs": list(r.refs or []),
            "holder_session": r.holder_session, "holder_account": r.holder_account,
            "last_progress_at": _iso(r.last_progress_at),
            "last_progress_text": r.last_progress_text,
            "next_checkpoint_at": _iso(r.next_checkpoint_at), "project": r.project,
            "depends_on": list(r.depends_on or []), "auto_pickup": bool(r.auto_pickup),
            "waiting_until": _iso(r.waiting_until), "waiting_reason": r.waiting_reason,
            "responsible": r.responsible}

"""Complete, read-only keyset scan of the Room task registry."""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from sqlalchemy import func, select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room.task_attention import tasks_with_attention
from vera_shared.timeutil import utc_naive_now


def _encode(room: str, high: str, after: str, seen: int, expected: int) -> str:
    payload = {"v": 1, "r": room, "h": high, "a": after, "n": seen, "e": expected}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str, room: str) -> tuple[str, str, int, int]:
    if len(cursor) > 1024:
        raise ValueError("inventory cursor is too long")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if (set(data) != {"v", "r", "h", "a", "n", "e"}
                or data["v"] != 1 or data["r"] != room
                or not isinstance(data["h"], str) or not isinstance(data["a"], str)
                or not isinstance(data["n"], int) or isinstance(data["n"], bool)
                or not isinstance(data["e"], int) or isinstance(data["e"], bool)
                or not 0 <= data["n"] < data["e"]
                or not data["a"] <= data["h"]):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
        raise ValueError("invalid inventory cursor") from exc
    return data["h"], data["a"], data["n"], data["e"]


async def page(*, room: str, limit: int, cursor: str | None = None) -> dict[str, Any]:
    """Return all task IDs exactly once from a bounded initial key range.

    Task IDs are immutable and indexed by (room, task_id). The expected count
    catches concurrent additions within the range. A mismatch requires a fresh
    scan; it is never reported as a complete inventory.
    """
    if not 1 <= limit <= 200:
        raise ValueError("inventory limit must be 1..200")
    async with get_session() as s:
        if cursor is None:
            expected, high = (await s.execute(select(
                func.count(), func.max(RoomTaskRow.task_id),
            ).where(RoomTaskRow.room == room))).one()
            after, seen = "", 0
        else:
            high, after, seen, expected = _decode(cursor, room)
        if high is None:
            return {"tasks": [], "next_cursor": None, "expected_count": 0,
                    "returned_count": 0, "complete": True, "inconsistent": False}
        rows = (await s.execute(select(RoomTaskRow).where(
            RoomTaskRow.room == room, RoomTaskRow.task_id > after,
            RoomTaskRow.task_id <= high,
        ).order_by(RoomTaskRow.task_id).limit(limit + 1))).scalars().all()
        more = len(rows) > limit
        rows = rows[:limit]
        tasks = await tasks_with_attention(s, list(rows), utc_naive_now())
    seen += len(tasks)
    inconsistent = (not more and seen != expected) or seen > expected
    complete = not more and not inconsistent
    next_cursor = (_encode(room, high, rows[-1].task_id, seen, expected)
                   if more and not inconsistent else None)
    return {"tasks": tasks, "next_cursor": next_cursor,
            "expected_count": expected, "returned_count": seen,
            "complete": complete, "inconsistent": inconsistent}

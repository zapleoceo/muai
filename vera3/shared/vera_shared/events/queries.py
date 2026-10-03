"""Чтение событий для API и MCP: свежие, одно событие целиком, сводка по источникам.

Скрытые события (`visibility.HIDDEN_STATUS`) в списках не показываются;
`get_event_detail` отдаёт их с пометкой, чтобы их можно было вернуть.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select, text

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.events.visibility import HIDDEN_STATUS
from vera_shared.timeutil import utc_naive_now

PREVIEW_CHARS = 300


def event_preview(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": row.source,
        "account": row.account,
        "occurred_at": row.occurred_at.isoformat(),
        "content_preview": (row.content_text or "")[:PREVIEW_CHARS],
        "importance": row.importance,
        "project": row.project,
    }


async def recent_events(
    *, hours: int, limit: int, source: str | None = None,
    account: str | None = None, project: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """(события новые→старые, truncated). truncated — упёрлись в `limit`."""
    since = utc_naive_now() - timedelta(hours=hours)
    q = select(EventRow).where(EventRow.occurred_at >= since,
                               EventRow.triage_status != HIDDEN_STATUS)
    if source:
        q = q.where(EventRow.source == source)
    if account:
        q = q.where(EventRow.account == account)
    if project:
        q = q.where(EventRow.project == project)
    async with get_session() as s:
        rows = (await s.execute(
            q.order_by(EventRow.occurred_at.desc()).limit(limit)
        )).scalars().all()
    return [event_preview(r) for r in rows], len(rows) == limit


async def get_event_row(event_id: int) -> EventRow | None:
    async with get_session() as s:
        return (await s.execute(
            select(EventRow).where(EventRow.id == event_id)
        )).scalar_one_or_none()


async def source_stats() -> list[dict[str, Any]]:
    """По каждому источнику: сколько событий, последнее событие и последний приём."""
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT source, COUNT(*) AS events,
                   MAX(occurred_at) AS last_occurred_at,
                   MAX(received_at) AS last_ingested_at,
                   SUM(CASE WHEN triage_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN triage_status = 'hidden' THEN 1 ELSE 0 END) AS hidden
            FROM events GROUP BY source ORDER BY COUNT(*) DESC
        """))).mappings().all()
    return [{"source": r["source"], "events": int(r["events"]),
             "last_occurred_at": _iso(r["last_occurred_at"]),
             "last_ingested_at": _iso(r["last_ingested_at"]),
             "pending": int(r["pending"] or 0), "hidden": int(r["hidden"] or 0)}
            for r in rows]


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (
        str(value) if value is not None else None)

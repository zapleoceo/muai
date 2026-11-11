"""Чтение Gmail-ящиков для фрагмента прогресса: только email и время последнего опроса."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import GmailAccountRow


async def list_gmail_poll_status() -> list[tuple[str, datetime | None]]:
    async with get_session() as s:
        rows = (await s.execute(
            select(GmailAccountRow.email, GmailAccountRow.last_polled_at)
            .order_by(GmailAccountRow.id)
        )).all()
    return [(email, polled) for email, polled in rows]

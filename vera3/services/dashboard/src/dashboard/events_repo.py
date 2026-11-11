"""SQL «Входящего» (`/events`) и карточки события (`/events/{id}`)."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

SEARCH_TIMEOUT_S = 5


def _is_postgres(session: Any) -> bool:
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    return getattr(dialect, "name", "") == "postgresql"


def _is_timeout(exc: DBAPIError) -> bool:
    msg = str(exc).lower()
    return "statement timeout" in msg or "canceling statement" in msg


async def fetch_event_page(where_sql: str, params: dict[str, Any],
                           bounded: bool) -> list[Any] | None:
    """Строки страницы; None — запрос не уложился в таймаут (текстовый поиск
    по events без индекса). `where_sql` собирается только из фиксированных
    фрагментов `events_service.events_filter`, значения — в `params`."""
    sql = text(f"""
        SELECT e.id, e.triage_status, e.importance, e.source, e.account,
               e.occurred_at, e.content_text, e.metadata, e.nature,
               EXISTS(SELECT 1 FROM event_embeddings ee WHERE ee.event_id = e.id) AS has_emb,
               u.request_id, u.model, u.tokens_in, u.tokens_out, u.cost_usd
        FROM events e
        LEFT JOIN LATERAL (
            SELECT request_id, model, tokens_in, tokens_out, cost_usd
            FROM usage_log ul
            WHERE ul.event_id = e.id
            ORDER BY ul.created_at DESC
            LIMIT 1
        ) u ON true
        {where_sql}
        ORDER BY e.occurred_at DESC, e.id DESC
        LIMIT :limit
    """)
    async with get_session() as s:
        try:
            if bounded and _is_postgres(s):
                await s.execute(text(f"SET LOCAL statement_timeout = '{SEARCH_TIMEOUT_S}s'"))
            return list((await s.execute(sql, params)).mappings().all())
        except DBAPIError as e:
            if not _is_timeout(e):
                raise
            log.warning("поиск во входящем не уложился в %sс", SEARCH_TIMEOUT_S)
            return None


async def get_event_card_row(event_id: int) -> Any | None:
    async with get_session() as s:
        return (await s.execute(text("""
            SELECT id, source, account, category, occurred_at, importance,
                   nature, project, triage_status, triage_error,
                   content_text, content_extra, metadata
            FROM events WHERE id = :id
        """), {"id": event_id})).mappings().first()

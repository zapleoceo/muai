"""Входящее (`/events`) — события по дням с фильтром, плюс карточка события
`/events/{id}`. Запрос и маршрутизация здесь, разметка списка — в `events_view`."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from vera_shared.db.engine import get_session
from vera_shared.timeutil import utc_naive_now

from dashboard.event_view import STREAM_LABEL, event_card, transcript_html  # noqa: F401
from dashboard.events_view import (  # noqa: F401
    EVENTS_COLUMN_HINTS,
    PAGE_STEP,
    TRIAGE_STATUS_INFO,
    events_table,
    filter_form,
    more_link,
    parse_cursor,
)
from dashboard.render import _render, owner_or_redirect
from dashboard.stats import get_stats

router = APIRouter()
log = logging.getLogger(__name__)

SEARCH_TIMEOUT_S = 5


def _like(raw: str) -> str:
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _is_postgres(session: Any) -> bool:
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    return getattr(dialect, "name", "") == "postgresql"


def _is_timeout(exc: DBAPIError) -> bool:
    msg = str(exc).lower()
    return "statement timeout" in msg or "canceling statement" in msg


async def _fetch(where_sql: str, params: dict[str, Any], bounded: bool) -> list[Any] | None:
    """Строки страницы; None — запрос не уложился в таймаут (текстовый поиск
    по events без индекса)."""
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


@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request,
                       limit: int = Query(PAGE_STEP, ge=1, le=PAGE_STEP * 4),  # noqa: B008
                       source: str | None = None,
                       status: str | None = None,
                       q: str = "",
                       tech: str = "",
                       before: str = ""):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    show_tech = tech == "1"
    needle = q.strip()
    where = []
    params: dict[str, Any] = {"limit": limit + 1}
    if source:
        where.append("e.source = :source")
        params["source"] = source
    if status:
        where.append("e.triage_status = :status")
        params["status"] = status
    if needle:
        where.append("e.content_text ILIKE :q ESCAPE '\\'")
        params["q"] = _like(needle)
    if (cursor := parse_cursor(before)) is not None:
        where.append("(e.occurred_at, e.id) < (:before_at, :before_id)")
        params["before_at"], params["before_id"] = cursor
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    fetched = await _fetch(where_sql, params, bounded=bool(needle))
    timed_out = fetched is None
    rows = fetched or []
    has_more = len(rows) > limit
    rows = rows[:limit]
    st = await get_stats()
    link = more_link({"source": source, "status": status, "q": needle, "limit": limit,
                      "tech": "1" if show_tech else ""}, rows[-1]) if has_more else ""
    notice = ('<p class="error">Слишком долгий поиск — уточните запрос '
              'или выберите источник.</p>' if timed_out else "")
    top = '<p><a href="/events">← к новым</a></p>' if cursor_given(before) else ""
    return HTMLResponse(_render("events", f"""
        <h2>Входящее</h2>
        {filter_form(st["sources_all"], source, status, needle, show_tech)}
        {notice}{top}
        {events_table(rows, utc_naive_now().date(), show_tech)}
        {link}
    """))


def cursor_given(before: str) -> bool:
    return parse_cursor(before) is not None


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_page(request: Request, event_id: int):
    """Карточка события: выжимка плюс дословная стенограмма, если она есть."""
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    async with get_session() as s:
        row = (await s.execute(text("""
            SELECT id, source, account, category, occurred_at, importance,
                   nature, project, triage_status, triage_error,
                   content_text, content_extra, metadata
            FROM events WHERE id = :id
        """), {"id": event_id})).mappings().first()

    if row is None:
        return HTMLResponse(_render("events", "<h2>Событие не найдено</h2>"), 404)

    return HTMLResponse(_render("events", event_card(row)))

"""Входящее (`/events`) — события по дням с фильтром, плюс карточка события
`/events/{id}`. Здесь только маршрутизация; выборка и фильтр — в
`events_service`, SQL — в `events_repo`, разметка — в `events_view`."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from vera_shared.timeutil import utc_naive_now

from dashboard.event_view import (  # noqa: F401
    STREAM_LABEL,
    event_card,
    mail_addresses,
    transcript_html,
)
from dashboard.events_service import load_event_card, load_events_page
from dashboard.events_view import (  # noqa: F401
    EVENTS_COLUMN_HINTS,
    PAGE_STEP,
    TRIAGE_STATUS_INFO,
    events_table,
    filter_form,
    more_link,
    parse_cursor,
)
from dashboard.redaction import owner_wants_raw
from dashboard.render import _render, owner_or_redirect
from dashboard.stats import get_stats

router = APIRouter()


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
    page = await load_events_page(limit, source, status, needle, before)
    st = await get_stats()
    link = more_link({"source": source, "status": status, "q": needle, "limit": limit,
                      "tech": "1" if show_tech else ""},
                     page.rows[-1]) if page.has_more else ""
    notice = ('<p class="error">Слишком долгий поиск — уточните запрос '
              'или выберите источник.</p>' if page.timed_out else "")
    top = '<p><a href="/events">← к новым</a></p>' if cursor_given(before) else ""
    return HTMLResponse(_render("events", f"""
        <h2>Входящее</h2>
        {filter_form(st["sources_all"], source, status, needle, show_tech)}
        {notice}{top}
        {events_table(page.rows, utc_naive_now().date(), show_tech)}
        {link}
    """))


def cursor_given(before: str) -> bool:
    return parse_cursor(before) is not None


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_page(request: Request, event_id: int):
    """Карточка события: выжимка плюс дословная стенограмма, если она есть."""
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    card = await load_event_card(event_id)
    if card is None:
        return HTMLResponse(_render("events", "<h2>Событие не найдено</h2>"), 404)
    row, people = card
    raw = owner_wants_raw(request)
    return HTMLResponse(_render("events", event_card(row, people, raw)),
                        headers={"Cache-Control": "no-store"})

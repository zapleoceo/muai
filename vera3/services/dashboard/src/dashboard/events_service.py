"""«Входящее» без HTTP: фильтр → страница событий, карточка события → её
участники, которых граф уже знает."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from vera_shared.graph.repo import find_entity_by_alias

from dashboard.event_text import clean_name, parse_content
from dashboard.event_view import mail_addresses
from dashboard.events_repo import fetch_event_page, get_event_card_row
from dashboard.events_view import parse_cursor


@dataclass
class EventsPage:
    rows: list[Any]
    has_more: bool
    timed_out: bool


def _like(raw: str) -> str:
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def events_filter(source: str | None, status: str | None, needle: str,
                  before: str) -> tuple[str, dict[str, Any]]:
    """WHERE из фиксированных фрагментов; всё пользовательское — только в params."""
    where = []
    params: dict[str, Any] = {}
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
    return ("WHERE " + " AND ".join(where)) if where else "", params


async def load_events_page(limit: int, source: str | None, status: str | None,
                           needle: str, before: str) -> EventsPage:
    where_sql, params = events_filter(source, status, needle, before)
    fetched = await fetch_event_page(where_sql, {"limit": limit + 1, **params},
                                     bounded=bool(needle))
    rows = fetched or []
    return EventsPage(rows=rows[:limit], has_more=len(rows) > limit,
                      timed_out=fetched is None)


async def load_event_card(event_id: int) -> tuple[dict[str, Any], dict[str, int]] | None:
    """Строка события и ссылки на известных графу участников; None — нет такого."""
    row = await get_event_card_row(event_id)
    if row is None:
        return None
    if isinstance(row["metadata"], str):      # SQLite отдаёт JSON строкой, Postgres — словарём
        row = {**row, "metadata": json.loads(row["metadata"])}
    return row, await _people_links(row)


async def _people_links(row: Any) -> dict[str, int]:
    """Кого из участников события граф уже знает: адрес (gmail) или автор по telegram id."""
    links: dict[str, int] = {}
    parsed = parse_content(row["content_text"])
    for address in mail_addresses(parsed):
        if entity := await find_entity_by_alias("gmail", address):
            links[address] = entity
    sender = (row["metadata"] or {}).get("sender_id")
    if sender and (entity := await find_entity_by_alias(row["source"], f"user:{sender}")):
        for key in ("Author", "From"):
            if name := clean_name(parsed.headers.get(key, "")):
                links[name.lower()] = entity
    return links

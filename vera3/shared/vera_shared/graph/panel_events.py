"""Последние события человека для карточки `/graph` — по ВСЕМ его алиасам.

Какой запрос какому алиасу:

* telegram `user:<id>` — сообщения, где он автор (`metadata.sender_id`, частичный
  индекс `ix_events_tg_sender`), и личная переписка с ним в обе стороны
  (`metadata.chat_id` = его id; индекса нет, поэтому окно `DM_WINDOW_DAYS` по
  `ix_events_occurred_at`);
* slack / instagram `user:<id>` — `metadata.sender_id`;
* gmail `<адрес>` — адрес в `metadata.from` ИЛИ `metadata.to`.

Скрытые события не показываются. Каждый запрос идёт в своей сессии под коротким
`statement_timeout`: сорвался один — остальные всё равно покажут свои события.
Результат — самые новые по всем источникам сразу. Текст — без цитат прежней переписки.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from vera_shared.db.engine import get_session
from vera_shared.events.visibility import NOT_HIDDEN_SQL
from vera_shared.ingest.envelope import HEADER_SEPARATOR, message_body
from vera_shared.ingest.quotes import strip_quoted
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

EVENTS_SHOWN = 5
SNIPPET_CHARS = 160
EVENTS_TIMEOUT_S = 2
DM_WINDOW_DAYS = 400
_SENDER_SOURCES = ("telegram", "slack", "instagram")
_SUBJECT = re.compile(r"^Subject:[ \t]*(.*)$", re.MULTILINE)
_COLUMNS = "SELECT id, source, occurred_at, content_text FROM events WHERE "
_TAIL = f" AND {NOT_HIDDEN_SQL} ORDER BY occurred_at DESC LIMIT :n"


def _is_postgres(session: Any) -> bool:
    return getattr(getattr(getattr(session, "bind", None), "dialect", None), "name", "") == "postgresql"


def _iso(value: datetime | str) -> str:
    # Сырой text() на SQLite отдаёт строку, на Postgres — datetime.
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _subject(content_text: str | None) -> str:
    head = (content_text or "").split(HEADER_SEPARATOR, 1)[0]
    found = _SUBJECT.search(head)
    return found.group(1).strip() if found else ""


def snippet(content_text: str | None) -> str:
    flat = " ".join(strip_quoted(message_body(content_text)).split())
    return flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS - 1].rstrip() + "…"


def queries_for(source: str, identifier: str, limit: int) -> list[tuple[str, dict[str, Any]]]:
    """SQL и параметры для одного алиаса; пусто — источник карточка не показывает."""
    if source == "gmail":
        key = f"%{identifier.lower()}%"
        return [(_COLUMNS + "source = 'gmail' AND (lower(metadata->>'from') LIKE :key "
                 "OR lower(metadata->>'to') LIKE :key)" + _TAIL, {"key": key, "n": limit})]
    if source not in _SENDER_SOURCES:
        return []
    key = identifier.removeprefix("user:")
    found = [(_COLUMNS + "source = :src AND metadata->>'sender_id' = :key" + _TAIL,
              {"src": source, "key": key, "n": limit})]
    if source == "telegram":
        since = utc_naive_now() - timedelta(days=DM_WINDOW_DAYS)
        found.append((_COLUMNS + "source = 'telegram' AND metadata->>'chat_id' = :key "
                      "AND occurred_at > :since" + _TAIL, {"key": key, "since": since, "n": limit}))
    return found


async def _run(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    async with get_session() as s:
        try:
            if _is_postgres(s):
                await s.execute(text(f"SET LOCAL statement_timeout = '{EVENTS_TIMEOUT_S}s'"))
            rows = (await s.execute(text(sql), params)).mappings().all()
        except DBAPIError as e:
            log.warning("панель: запрос событий не уложился в %sс: %s", EVENTS_TIMEOUT_S, e)
            return []
    return [{"id": r["id"], "source": r["source"], "occurred_at": _iso(r["occurred_at"]),
             "subject": _subject(r["content_text"]) if r["source"] == "gmail" else "",
             "snippet": snippet(r["content_text"])} for r in rows]


async def recent_events(aliases: list[tuple[str, str]], limit: int = EVENTS_SHOWN) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for source, identifier in aliases:
        for sql, params in queries_for(source, identifier, limit):
            for event in await _run(sql, params):
                found[event["id"]] = event
    return sorted(found.values(), key=lambda e: e["occurred_at"], reverse=True)[:limit]

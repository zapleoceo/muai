"""Список «На чём основан ответ» под ответом поиска: человеческая строка
вместо сырого заголовка письма или сообщения."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from dashboard.event_text import (
    EventLine,
    describe,
    normalize_subject,
    parse_content,
    snippet_of,
)
from dashboard.render import esc, local_dt
from dashboard.source_registry import resolve_source

SOURCES_SHOWN = 5


def _when(iso: str) -> str:
    try:
        return local_dt(datetime.fromisoformat(iso), "date_human")
    except (TypeError, ValueError):
        return esc(iso)


def headline(source: str, line: EventLine, title: str) -> str:
    if source == "gmail" and line.subject:
        sender = f" · от {line.who}" if line.who else ""
        return f"{line.subject}{sender}"
    parts = [p for p in (line.who, line.venue) if p]
    return " · ".join(parts) or title


def dedupe_key(source: str, line: EventLine) -> tuple[str, str] | None:
    """Письма одной цепочки (Re:/Fwd:) — один источник ответа, а не три."""
    if line.subject:
        return source, normalize_subject(line.subject)
    return None


def sources_html(results: list[dict[str, Any]]) -> str:
    """Пять самых близких событий — то, на чём стоит ответ. Пусто, если
    поиск ничего не вернул (например, расчётный ответ без LLM)."""
    items: list[str] = []
    seen_ids: set[int] = set()
    last_key: tuple[str, str] | None = None
    for r in results:
        if len(items) >= SOURCES_SHOWN:
            break
        event_id = r.get("event_id")
        if not isinstance(event_id, int) or event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        key_source = str(r.get("source") or "")
        src = resolve_source(key_source)
        line = describe(parse_content(r.get("content_preview")))
        key = dedupe_key(key_source, line)
        if key is not None and key == last_key:
            continue
        last_key = key
        snippet = snippet_of(line.body)
        tail = f'<div class="muted small">{esc(snippet)}</div>' if snippet else ""
        items.append(
            f'<li><a href="/events/{event_id}">{src.icon} {esc(headline(key_source, line, src.title))}</a>'
            f' <span class="muted small">· {_when(r.get("occurred_at") or "")}</span>{tail}</li>')
    if not items:
        return ""
    return f'<h4>На чём основан ответ</h4><ul class="sources">{"".join(items)}</ul>'

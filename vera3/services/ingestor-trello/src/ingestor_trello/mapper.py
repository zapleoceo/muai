"""Действие Trello → спецификация строки events. Чистая функция, без сети и БД.

Контракт авторства (docs/sources.md): первая строка content_text — `Author:`,
в metadata обязаны быть author_role и author_label. Для Trello авторство
однозначное: action.idMemberCreator сравнивается с id владельца.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from vera_shared.timeutil import utc_naive_now

from ingestor_trello.describe import category, describe

MAX_CONTENT_LEN = 8000


def parse_date(value: str | None) -> datetime:
    """ISO-время Trello (…Z) → наивный UTC, как во всех остальных источниках."""
    text = str(value or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return utc_naive_now()
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _author(action: dict[str, Any], me_id: str) -> tuple[str, str, str | None]:
    """→ (author_role, author_label, username автора если не я)."""
    creator = action.get("memberCreator") or {}
    if str(action.get("idMemberCreator") or creator.get("id") or "") == me_id:
        return "self", "Я", None
    username = str(creator.get("username") or "") or None
    label = str(creator.get("fullName") or username or "участник Trello")
    return "counterparty", label, username


def action_to_event(
    action: dict[str, Any],
    *,
    me_id: str,
    me_username: str,
    board_name: str,
) -> dict[str, Any] | None:
    """Спецификация EventRow, либо None — если действие не стоит события."""
    body = describe(action)
    if not body:
        return None
    action_id = str(action.get("id") or "")
    if not action_id:
        return None

    data = action.get("data") or {}
    card = data.get("card") or {}
    board = data.get("board") or {}
    author_role, author_label, author_username = _author(action, me_id)
    occurred = parse_date(action.get("date"))
    short_link = card.get("shortLink")

    content = (
        f"Author: {author_label} [{author_role}]\n"
        f"Board: {board.get('name') or board_name}\n"
        f"Card: {card.get('name') or '—'}\n"
        f"Date: {action.get('date') or ''}\n"
        f"---\n{body}"
    )[:MAX_CONTENT_LEN]

    hints: list[dict[str, Any]] = []
    if author_username:
        hints.append({
            "type": "person",
            "identifier": author_username,
            "name": author_label,
        })

    return {
        "source": "trello",
        "source_event_id": action_id,
        "account": me_username,
        "category": category(str(action.get("type") or "")),
        "content_text": content,
        "occurred_at": occurred,
        "entity_hints": hints,
        "metadata_": {
            "author_role": author_role,
            "author_label": author_label,
            "author_username": author_username,
            "action_type": action.get("type"),
            "board_id": board.get("id"),
            "board_name": board.get("name") or board_name,
            "card_id": card.get("id"),
            "card_name": card.get("name"),
            "card_url": f"https://trello.com/c/{short_link}" if short_link else None,
            "list_before": (data.get("listBefore") or {}).get("name"),
            "list_after": (data.get("listAfter") or {}).get("name"),
            "due": card.get("due"),
        },
    }

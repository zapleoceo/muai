"""Связь событий и сущностей. Автор события выводится по алиасу, остальное — из индекса `event_entities`.

| источник | алиас сущности | где в событии |
|---|---|---|
| telegram, slack, instagram | `user:<id>` | `metadata->>'sender_id'` |
| gmail | адрес почты | `metadata->>'from'` |

Плюс связи графа, выведенные из события (`relationships.derived_from_event_id`),
и упоминание полного имени сущности в тексте. С миграцией 042 есть и явная таблица
`event_entities` (автор, получатель, участник, упомянутый — `vera_shared.links`);
`linked_entities` её тоже читает, а `timeline_events` остаётся прежним запасным путём.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.events.queries import PREVIEW_CHARS
from vera_shared.events.visibility import NOT_HIDDEN_SQL

_BY_SENDER_ID = ("telegram", "slack", "instagram")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
#: Короткое имя («Ли», «Дима») в тексте — шум, а не упоминание.
MIN_MENTION_NAME = 4


def _like(value: str) -> str:
    return "%" + value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _indexed_links(s: AsyncSession, event_id: int) -> list[Any]:
    """Связи из индекса `event_entities`; пусто, пока миграция 042 не накачена
    (SAVEPOINT: сбой запроса не должен рушить транзакцию вызывающего)."""
    try:
        async with s.begin_nested():
            return list((await s.execute(text(
                "SELECT e.id, e.name, e.type, l.role, l.source_of_link, l.confidence "
                "FROM event_entities l JOIN entities e ON e.id = l.entity_id "
                "WHERE l.event_id = :id AND l.scope_ok ORDER BY l.confidence DESC"),
                {"id": event_id})).mappings())
    except DBAPIError:
        return []


async def linked_entities(event_id: int) -> list[dict[str, Any]]:
    """Автор события (по алиасу) и концы связей, выведенных из него."""
    async with get_session() as s:
        ev = (await s.execute(
            text("SELECT source, metadata->>'sender_id' AS sender, "
                 "lower(metadata->>'from') AS sender_from "
                 "FROM events WHERE id = :id"), {"id": event_id})).mappings().first()
        if ev is None:
            return []
        found: dict[int, dict[str, Any]] = {}
        keys: list[tuple[str, str]] = []
        if ev["source"] in _BY_SENDER_ID and ev["sender"]:
            keys.append((ev["source"], f"user:{ev['sender']}"))
        if ev["source"] == "gmail" and ev["sender_from"]:
            keys += [("gmail", a) for a in _EMAIL_RE.findall(ev["sender_from"])]
        for source, identifier in keys:
            for r in (await s.execute(text(
                "SELECT e.id, e.name, e.type FROM entities e "
                "JOIN entity_aliases a ON a.entity_id = e.id "
                "WHERE a.source = :s AND lower(a.identifier) = :i"),
                {"s": source, "i": identifier.lower()})).mappings():
                found.setdefault(r["id"], {"entity_id": r["id"], "name": r["name"],
                                           "type": r["type"], "via": "author"})
        for r in (await s.execute(text(
            "SELECT e.id, e.name, e.type FROM relationships r "
            "JOIN entities e ON e.id IN (r.subject_entity_id, r.object_entity_id) "
            "WHERE r.derived_from_event_id = :id"), {"id": event_id})).mappings():
            found.setdefault(r["id"], {"entity_id": r["id"], "name": r["name"],
                                       "type": r["type"], "via": "relationship"})
        for r in await _indexed_links(s, event_id):
            item = found.setdefault(r["id"], {"entity_id": r["id"], "name": r["name"],
                                              "type": r["type"], "via": r["role"]})
            item.setdefault("roles", []).append(
                {"role": r["role"], "via": r["source_of_link"],
                 "confidence": round(float(r["confidence"]), 2)})
    return list(found.values())


async def timeline_events(entity_id: int, start: datetime, end: datetime,
                          limit: int) -> list[dict[str, Any]]:
    """События сущности за [start, end): написанные ею или с её именем в тексте."""
    async with get_session() as s:
        ent = (await s.execute(text("SELECT name FROM entities WHERE id = :id"),
                               {"id": entity_id})).first()
        if ent is None:
            return []
        aliases = (await s.execute(text(
            "SELECT source, identifier FROM entity_aliases WHERE entity_id = :id"),
            {"id": entity_id})).all()
        conds: list[str] = []
        params: dict[str, Any] = {"a": start, "b": end, "lim": limit}
        for i, (source, identifier) in enumerate(aliases):
            if source in _BY_SENDER_ID and identifier.startswith("user:"):
                conds.append(f"(source = :s{i} AND metadata->>'sender_id' = :i{i})")
                params[f"s{i}"], params[f"i{i}"] = source, identifier[5:]
            elif source == "gmail":
                conds.append(f"(source = 'gmail' AND lower(metadata->>'from') "
                             f"LIKE :i{i} ESCAPE '\\')")
                params[f"i{i}"] = _like(identifier)
        if len(ent[0] or "") >= MIN_MENTION_NAME:
            conds.append("lower(content_text) LIKE :nm ESCAPE '\\'")
            params["nm"] = _like(ent[0])
        if not conds:
            return []
        stmt = text(f"""
            SELECT id, source, account, occurred_at, content_text, importance, project
            FROM events
            WHERE {NOT_HIDDEN_SQL} AND occurred_at >= :a AND occurred_at < :b
              AND ({" OR ".join(conds)})
            ORDER BY occurred_at DESC LIMIT :lim
        """)
        rows = (await s.execute(stmt, params)).mappings().all()
    return [{"id": r["id"], "source": r["source"], "account": r["account"],
             "occurred_at": str(r["occurred_at"]),
             "content_preview": (r["content_text"] or "")[:PREVIEW_CHARS],
             "importance": r["importance"], "project": r["project"]} for r in rows]

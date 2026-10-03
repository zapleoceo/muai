"""Карточка сущности для боковой панели `/graph`: алиасы, счётчики, связи-пары
и последние события человека.

События ищутся по ВСЕМ алиасам человека (`panel_events`): telegram, slack, instagram,
gmail; каждый запрос под коротким таймаутом, чтобы панель не висела на скане большой
таблицы.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.graph.connections import CONNECTIONS_LIMIT, entity_connections
from vera_shared.graph.panel_events import (  # noqa: F401
    EVENTS_SHOWN,
    SNIPPET_CHARS,
    recent_events,
    recent_events_status,
)
from vera_shared.graph.repo import get_entity, list_relationships

log = logging.getLogger(__name__)

CONNECTIONS_SHOWN = 12
RAW_RELATIONSHIPS_SHOWN = 8
#: что показываем из свободных атрибутов сущности, в порядке чтения
PROFILE_KEYS = ("employer", "role", "location")


async def entity_aliases(entity_id: int) -> list[tuple[str, str]]:
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT source, identifier FROM entity_aliases WHERE entity_id = :e "
                 "ORDER BY source, identifier"), {"e": entity_id})).all()
    return [(r[0], r[1]) for r in rows]


async def _counts(entity_id: int) -> dict[str, int]:
    async with get_session() as s:
        relationships = (await s.execute(text(
            "SELECT count(*) FROM relationships WHERE is_current AND "
            "(subject_entity_id = :e OR object_entity_id = :e)"), {"e": entity_id})).scalar_one()
        groups = (await s.execute(text(
            "SELECT count(*) FROM memberships WHERE is_current AND child_entity_id = :e"),
            {"e": entity_id})).scalar_one()
        members = (await s.execute(text(
            "SELECT count(*) FROM memberships WHERE is_current AND parent_entity_id = :e"),
            {"e": entity_id})).scalar_one()
    return {"relationships": int(relationships), "groups": int(groups), "members": int(members)}


def _relationship(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("predicate", "direction", "other_id", "other_name", "other_type")
    return {k: row[k] for k in keys}


async def entity_panel(entity_id: int, *, raw: bool = False, with_events: bool = True,
                       connections_limit: int = CONNECTIONS_SHOWN) -> dict[str, Any] | None:
    """Всё для панели одним ответом; None — такой сущности нет. `connections` — по
    одной связи на собеседника (`connections.entity_connections`); `raw=True` добавляет
    ещё и записи `relationships` по одной. `with_events=False` — без событий (самая медленная
    часть: страница берёт их отдельным запросом, и карточка рисуется сразу); `connections_total`
    — сколько людей связано всего (до `CONNECTIONS_LIMIT`), `connections` — первые
    `connections_limit`."""
    entity = await get_entity(entity_id)
    if entity is None:
        return None
    attrs = entity.attributes or {}
    aliases, counts, conns = await asyncio.gather(
        entity_aliases(entity_id), _counts(entity_id),
        entity_connections(entity_id, limit=CONNECTIONS_LIMIT))
    payload: dict[str, Any] = {
        "id": entity.id, "name": entity.name, "type": entity.type,
        "username": attrs.get("username") or None,
        "tg_id": attrs.get("tg_id") or None,
        "email": attrs.get("email") or None,
        "profile": [str(attrs[k]) for k in PROFILE_KEYS if attrs.get(k)],
        "aliases": [{"source": s, "identifier": i} for s, i in aliases],
        "counts": counts,
        "connections": conns[:connections_limit],
        "connections_total": len(conns),
        "events": [], "events_partial": False,
    }
    if with_events:
        payload["events"], payload["events_partial"] = await recent_events_status(aliases)
    if raw:
        rels = await list_relationships(entity_id, limit=RAW_RELATIONSHIPS_SHOWN)
        payload["relationships"] = [_relationship(r) for r in rels]
    return payload

"""Сводка по сущности: алиасы, членства, связи, недавняя активность.

Раньше собиралась прямо в `gateway/query.py`; теперь шлюз и MCP-сервер берут
одну и ту же функцию.
"""
from __future__ import annotations

from typing import Any

from vera_shared.graph.connections import entity_connections
from vera_shared.graph.dedup import get_entity_context
from vera_shared.graph.repo import get_entity, list_members, list_relationships
from vera_shared.links.read import mention_counts, mentioning_events

RELATIONSHIPS_LIMIT = 50
MENTIONS_LIMIT = 20


async def entity_context_payload(entity_id: int, *,
                                 raw_relationships: bool = True,
                                 include_mentions: bool = False) -> dict[str, Any] | None:
    """None, если сущности нет. `connections` — по одной связи на собеседника
    (роли, вес, взаимодействия); `raw_relationships` добавляет записи `relationships`
    по одной (с id — их снимает `relationship_retire`); `include_mentions` — события, где
    человека УПОМЯНУЛИ (не его сообщения), и счёт «в области / вне области»."""
    entity = await get_entity(entity_id)
    if entity is None:
        return None
    ctx = await get_entity_context(entity_id)
    members = await list_members(entity_id)
    payload: dict[str, Any] = {
        "entity_id": entity_id,
        "name": entity.name,
        "type": entity.type,
        "canonical_id": entity.canonical_id,
        "attributes": entity.attributes,
        "last_seen_at": entity.last_seen_at.isoformat() if entity.last_seen_at else None,
        "aliases": ctx["aliases"],
        "memberships": ctx["memberships"],
        "members": members,
        "recent_30d_messages": ctx["recent_30d_messages"],
        "connections": await entity_connections(entity_id, limit=RELATIONSHIPS_LIMIT),
    }
    if raw_relationships:
        payload["relationships"] = await list_relationships(entity_id, limit=RELATIONSHIPS_LIMIT)
    if include_mentions:
        payload["mentions"] = await mentioning_events(entity_id, MENTIONS_LIMIT)
        payload["mention_counts"] = await mention_counts(entity_id)
    return payload

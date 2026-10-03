"""Сводка по сущности: алиасы, членства, связи, недавняя активность.

Раньше собиралась прямо в `gateway/query.py`; теперь шлюз и MCP-сервер берут
одну и ту же функцию.
"""
from __future__ import annotations

from typing import Any

from vera_shared.graph.dedup import get_entity_context
from vera_shared.graph.repo import get_entity, list_members, list_relationships

RELATIONSHIPS_LIMIT = 50


async def entity_context_payload(entity_id: int) -> dict[str, Any] | None:
    """None, если сущности нет."""
    entity = await get_entity(entity_id)
    if entity is None:
        return None
    rel_rows = await list_relationships(entity_id, limit=RELATIONSHIPS_LIMIT)
    ctx = await get_entity_context(entity_id)
    members = await list_members(entity_id)
    return {
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
        "relationships": rel_rows,
    }

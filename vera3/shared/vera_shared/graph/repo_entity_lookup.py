"""Чтение сущностей: поиск по имени/алиасу, точный резолв, сущность по id и
её связи в обе стороны."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityAliasRow, EntityRow


async def find_entity_by_name(name: str, type: str | None = None) -> int | None:
    """Fuzzy lookup. Useful for `tools.search_entities`."""
    async with get_session() as s:
        q = select(EntityRow.id).where(EntityRow.name.ilike(f"%{name}%"))
        if type:
            q = q.where(EntityRow.type == type)
        return (await s.execute(q.limit(1))).scalar_one_or_none()


async def find_entity_by_alias(source: str, identifier: str) -> int | None:
    async with get_session() as s:
        return (await s.execute(
            select(EntityAliasRow.entity_id).where(
                EntityAliasRow.source == source,
                EntityAliasRow.identifier == identifier,
            )
        )).scalar_one_or_none()


async def resolve_entity_exact(name: str) -> int | None:
    """Exact (case-insensitive) resolve by entity name, then alias
    display_name — but ONLY when the match is unambiguous.

    With 21 разных «Дима» in the graph, the old `.limit(1)` (no ORDER BY)
    attached rel-extract edges to an arbitrary namesake — non-deterministic
    and, worse, it silently fused different people into one node. Ambiguous
    names now resolve to None: rel_extract skips the edge rather than
    polluting the graph. Distinct from find_entity_by_name's fuzzy ILIKE —
    this path needs precision, not recall."""
    n = name.strip()
    async with get_session() as s:
        ids = list((await s.execute(
            select(EntityRow.id)
            .where(func.lower(EntityRow.name) == func.lower(n))
            .limit(2)
        )).scalars().all())
        if len(ids) == 1:
            return ids[0]
        if len(ids) > 1:
            return None   # namesakes — ambiguous, refuse to guess
        alias_ids = list((await s.execute(
            select(EntityAliasRow.entity_id).distinct()
            .where(func.lower(EntityAliasRow.display_name) == func.lower(n))
            .limit(2)
        )).scalars().all())
        return alias_ids[0] if len(alias_ids) == 1 else None


async def get_entity(entity_id: int) -> EntityRow | None:
    """Сущность по id. Существует, чтобы вызывающему не приходилось открывать
    свою сессию и трогать таблицу напрямую (gateway так и делал)."""
    async with get_session() as s:
        return (await s.execute(
            select(EntityRow).where(EntityRow.id == entity_id)
        )).scalar_one_or_none()


async def list_relationships(entity_id: int, limit: int = 40) -> list[dict[str, Any]]:
    """Связи сущности в обе стороны, с именем и типом второго конца.

    Жила в `gateway/query.py` сырым SQL с двумя JOIN'ами по `entities` —
    то есть route-модуль ходил в графовые таблицы мимо этого слоя.
    """
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT r.id, r.predicate, r.fact, r.confidence,
                   CASE WHEN r.subject_entity_id = :eid THEN 'out' ELSE 'in' END AS direction,
                   CASE WHEN r.subject_entity_id = :eid THEN eo.id ELSE es.id END AS other_id,
                   CASE WHEN r.subject_entity_id = :eid THEN eo.name ELSE es.name END AS other_name,
                   CASE WHEN r.subject_entity_id = :eid THEN eo.type ELSE es.type END AS other_type
            FROM relationships r
            JOIN entities es ON es.id = r.subject_entity_id
            JOIN entities eo ON eo.id = r.object_entity_id
            WHERE (r.subject_entity_id = :eid OR r.object_entity_id = :eid)
              AND r.is_current
            ORDER BY r.confidence DESC
            LIMIT :limit
        """), {"eid": entity_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]

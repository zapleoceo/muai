"""Нечёткий поиск сущностей: имя, алиас, username, email — одним запросом."""
from __future__ import annotations

from typing import Any

from sqlalchemy import String, cast, func, or_, select

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityAliasRow, EntityRow

_ESC = "\\"


def _like_pattern(q: str) -> str:
    safe = q.lower().replace(_ESC, _ESC * 2).replace("%", _ESC + "%").replace("_", _ESC + "_")
    return f"%{safe}%"


async def search_entities(q: str, *, limit: int = 20,
                          type: str | None = None) -> list[dict[str, Any]]:
    """Точное совпадение имени — первым, затем самые недавно виденные.

    Атрибуты ищутся как текст: username и email лежат в `attributes`.
    """
    pat = _like_pattern(q)
    alias_hit = select(EntityAliasRow.entity_id).where(or_(
        func.lower(EntityAliasRow.identifier).like(pat, escape=_ESC),
        func.lower(EntityAliasRow.display_name).like(pat, escape=_ESC),
    ))
    cond = or_(
        func.lower(EntityRow.name).like(pat, escape=_ESC),
        func.lower(cast(EntityRow.attributes, String)).like(pat, escape=_ESC),
        EntityRow.id.in_(alias_hit),
    )
    stmt = select(EntityRow).where(cond)
    if type:
        stmt = stmt.where(EntityRow.type == type)
    exact_first = (func.lower(EntityRow.name) == q.lower()).desc()
    async with get_session() as s:
        rows = (await s.execute(
            stmt.order_by(exact_first, EntityRow.last_seen_at.desc()).limit(limit)
        )).scalars().all()
    return [{"id": r.id, "name": r.name, "type": r.type, "canonical_id": r.canonical_id,
             "username": (r.attributes or {}).get("username"),
             "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None}
            for r in rows]

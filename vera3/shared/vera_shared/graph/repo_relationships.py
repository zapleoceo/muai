"""Чтение и пометка связей для аудита графа (`scripts/quarantine_junk_rels.py`).

Пометка — `is_current = false`, а не DELETE: мусорная связь перестаёт
показываться (`list_relationships`, `graph_snapshot`, степень в аватарах
фильтруют `is_current`), но остаётся в таблице с фактом и событием-источником,
и пометку можно снять по сохранённому списку id.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session


async def list_current_relationships_page(after_id: int, limit: int) -> list[dict[str, Any]]:
    """Текущие связи с именами и типами обоих концов, keyset по id."""
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT r.id, r.predicate, r.confidence, r.fact,
                   es.name AS subject_name, es.type AS subject_type,
                   eo.name AS object_name, eo.type AS object_type
            FROM relationships r
            JOIN entities es ON es.id = r.subject_entity_id
            JOIN entities eo ON eo.id = r.object_entity_id
            WHERE r.is_current AND r.id > :after
            ORDER BY r.id
            LIMIT :lim
        """), {"after": after_id, "lim": limit})).mappings().all()
    return [dict(r) for r in rows]


async def set_relationships_current(ids: list[int], *, current: bool) -> int:
    """Проставить `is_current` пачке связей. Возвращает число изменённых строк."""
    if not ids:
        return 0
    async with get_session() as s:
        res = await s.execute(
            text("UPDATE relationships SET is_current = :cur WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"cur": current, "ids": ids},
        )
    return res.rowcount or 0

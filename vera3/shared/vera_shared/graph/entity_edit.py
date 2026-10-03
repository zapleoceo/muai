"""Точечные правки сущности с отчётом для отката: тип и имя.

Нужны разбору дублей: сервисный отправитель, заведённый как person, не
сливается ни с кем, его надо перевести в organization; организации с именем
из local-part («noreply») — переименовать по домену.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityRow
from vera_shared.graph.merge_errors import MergeError

ENTITY_TYPES = frozenset({
    "person", "organization", "bot", "group", "supergroup", "channel",
    "account", "project", "place", "label", "other",
})


async def _edit(s: AsyncSession, entity_id: int, field: str, value: str) -> dict[str, Any]:
    row = await s.get(EntityRow, entity_id)
    if row is None:
        raise MergeError(f"сущность {entity_id} не найдена")
    old = getattr(row, field)
    setattr(row, field, value)
    await s.flush()
    return {"entity_id": entity_id, "field": field, "old": old, "new": value}


async def _edit_in(session: AsyncSession | None, entity_id: int, field: str,
                   value: str) -> dict[str, Any]:
    if session is not None:
        return await _edit(session, entity_id, field, value)
    async with get_session() as s:
        return await _edit(s, entity_id, field, value)


async def retype_entity(entity_id: int, new_type: str, *,
                        session: AsyncSession | None = None) -> dict[str, Any]:
    """Сменить тип. Возвращает отчёт `{entity_id, field, old, new}`."""
    if new_type not in ENTITY_TYPES:
        raise MergeError(f"неизвестный тип сущности: {new_type!r}")
    return await _edit_in(session, entity_id, "type", new_type)


async def rename_entity(entity_id: int, new_name: str, *,
                        session: AsyncSession | None = None) -> dict[str, Any]:
    if not new_name.strip():
        raise MergeError("пустое имя")
    return await _edit_in(session, entity_id, "name", new_name.strip())


async def revert_edit(report: dict[str, Any], *,
                      session: AsyncSession | None = None) -> None:
    """Откат `retype_entity` / `rename_entity` по их отчёту."""
    await _edit_in(session, report["entity_id"], report["field"], report["old"])

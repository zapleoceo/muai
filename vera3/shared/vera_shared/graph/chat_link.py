"""Группа Telegram, ставшая супергруппой: одна сущность, а не две.

При миграции Telegram меняет id чата: у старой `Chat` появляется
`migrated_to` (канал-супергруппа), новая `Channel` приходит с другим id. Без
связи граф получал две одноимённые сущности (group + supergroup, аудит
2026-09-26: 9 пар). Здесь legacy-чат либо «повышается» на месте, либо получает
алиас уже существующей супергруппы — и обе половины истории сходятся в одной.

Если обе сущности уже существуют по отдельности, ничего не сливаем (слияние —
не дело ингестора), а кладём пару в `merge_suggestions`.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityAliasRow, EntityRow
from vera_shared.graph.repo import upsert_entity
from vera_shared.graph.suggestions import propose_merge
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)


def _supergroup_attrs(new_id: int, old_id: int) -> dict[str, Any]:
    return {"tg_id": new_id, "tg_type": "channel", "is_megagroup": True,
            "migrated_from": old_id}


async def _alias(s: Any, identifier: str) -> EntityAliasRow | None:
    return (await s.execute(select(EntityAliasRow).where(
        EntityAliasRow.source == "telegram",
        EntityAliasRow.identifier == identifier))).scalar_one_or_none()


async def _link_known(s: Any, old: EntityAliasRow | None, new: EntityAliasRow | None,
                      old_id: int, new_id: int, title: str) -> int | None:
    """Случаи, где хватает одной стороны. None — нужен разбор конфликта/создание."""
    if new is not None and old is None:
        s.add(EntityAliasRow(entity_id=new.entity_id, source="telegram",
                             identifier=f"chat:{old_id}", display_name=title))
        return new.entity_id
    if old is not None and new is None:
        ent = (await s.execute(select(EntityRow).where(
            EntityRow.id == old.entity_id))).scalar_one()
        ent.type = "supergroup"
        ent.attributes = {**(ent.attributes or {}), **_supergroup_attrs(new_id, old_id)}
        ent.last_seen_at = utc_naive_now()
        s.add(EntityAliasRow(entity_id=ent.id, source="telegram",
                             identifier=f"chat:{new_id}", display_name=title))
        log.info("graph: группа %s переехала в супергруппу %s — одна сущность %s",
                 old_id, new_id, ent.id)
        return ent.id
    return None


async def resolve_migrated_chat(old_id: int, new_id: int, title: str) -> int:
    """Сущность чата для старой группы `old_id`, переехавшей в `new_id`."""
    async with get_session() as s:
        old = await _alias(s, f"chat:{old_id}")
        new = await _alias(s, f"chat:{new_id}")
        linked = await _link_known(s, old, new, old_id, new_id, title)
        pair = (old.entity_id, new.entity_id) if old and new else None
    if linked is not None:
        return linked
    if pair is not None:
        if pair[0] != pair[1]:
            await propose_merge(
                *pair, confidence=0.95,
                reason="Telegram: группа переехала в супергруппу (migrated_to)")
        return pair[1]
    entity_id = await upsert_entity(
        type="supergroup", name=title, source="telegram", identifier=f"chat:{new_id}",
        attributes=_supergroup_attrs(new_id, old_id))
    async with get_session() as s:
        s.add(EntityAliasRow(entity_id=entity_id, source="telegram",
                             identifier=f"chat:{old_id}", display_name=title))
    return entity_id

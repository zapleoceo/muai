"""Что мешает слить сущности без явного `force`: владелец и личность."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_graph import EntityAliasRow, EntityRow, IdentityNodeRow
from vera_shared.projects.rules import OWNER_TG_ID


class MergeBlocked(ValueError):
    """Слияние отклонено до `force=true`; сообщение перечисляет причины."""


async def entity_names(s: AsyncSession, ids: list[int]) -> dict[int, str]:
    rows = (await s.execute(
        select(EntityRow.id, EntityRow.name).where(EntityRow.id.in_(ids)))).all()
    return {r[0]: r[1] for r in rows}


async def merge_blockers(s: AsyncSession, ids: list[int]) -> list[str]:
    """Причины, по которым слияние требует `force`; пустой список — можно."""
    names = await entity_names(s, ids)
    reasons: list[str] = []
    owner = (await s.execute(
        select(EntityAliasRow.entity_id).where(
            EntityAliasRow.source == "telegram",
            EntityAliasRow.identifier == f"user:{OWNER_TG_ID}"))).scalar_one_or_none()
    if owner in ids:
        reasons.append(f"entity {owner} ({names.get(owner)}) is the owner")
    with_nodes = (await s.execute(
        select(IdentityNodeRow.listener_entity_id).where(
            IdentityNodeRow.listener_entity_id.in_(ids)).distinct())).scalars().all()
    reasons += [f"entity {i} ({names.get(i)}) has identity nodes" for i in sorted(with_nodes)]
    return reasons

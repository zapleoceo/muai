"""Ручная правка графа: имя сущности, алиасы, связи — в сессии вызывающего.

Сессию открывает вызывающий (правка + запись аудита одной транзакцией);
строки берутся `FOR UPDATE`, чтобы параллельные правки и откаты не затирали друг друга.
Слияние сущностей здесь НЕТ: это отдельная разрушительная операция
(`graph.dedup.merge_entities`).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_graph import EntityAliasRow, EntityRow, RelationshipRow
from vera_shared.graph.rel_extract import PREDICATES
from vera_shared.graph.rel_insert import insert_relationship_if_absent
from vera_shared.timeutil import utc_naive_now


class GraphEditError(ValueError):
    """Недопустимая правка графа: нет сущности, неизвестный предикат, конфликт."""


async def _entity(s: AsyncSession, entity_id: int) -> EntityRow:
    row = (await s.execute(
        select(EntityRow).where(EntityRow.id == entity_id).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        raise GraphEditError(f"entity {entity_id} not found")
    return row


async def rename_entity(s: AsyncSession, entity_id: int,
                        name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = await _entity(s, entity_id)
    before = {"name": row.name}
    row.name = name
    await s.flush()
    return before, {"name": row.name}


async def add_alias(s: AsyncSession, entity_id: int, source: str,
                    identifier: str, display_name: str | None) -> tuple[int, bool]:
    """(alias_id, created). Алиас уже у ЭТОЙ сущности — created=False."""
    await _entity(s, entity_id)
    found = (await s.execute(
        select(EntityAliasRow).where(EntityAliasRow.source == source,
                                     EntityAliasRow.identifier == identifier)
    )).scalar_one_or_none()
    if found is not None:
        if found.entity_id != entity_id:
            raise GraphEditError(
                f"alias {source}:{identifier} already belongs to entity "
                f"{found.entity_id} (needs a merge, not an alias)")
        return found.id, False
    alias = EntityAliasRow(entity_id=entity_id, source=source, identifier=identifier,
                           display_name=display_name, confidence=1.0)
    s.add(alias)
    await s.flush()
    return alias.id, True


async def alias_owner(s: AsyncSession, alias_id: int) -> int | None:
    return (await s.execute(
        select(EntityAliasRow.entity_id).where(EntityAliasRow.id == alias_id)
    )).scalar_one_or_none()


async def remove_alias(s: AsyncSession, alias_id: int) -> bool:
    alias = (await s.execute(
        select(EntityAliasRow).where(EntityAliasRow.id == alias_id).with_for_update()
    )).scalar_one_or_none()
    if alias is None:
        return False
    await s.delete(alias)
    return True


def relationship_snapshot(row: RelationshipRow) -> dict[str, Any]:
    return {"subject_entity_id": row.subject_entity_id,
            "object_entity_id": row.object_entity_id, "predicate": row.predicate,
            "fact": row.fact, "confidence": row.confidence,
            "is_current": row.is_current}


async def _relationship(s: AsyncSession, rel_id: int) -> RelationshipRow:
    row = (await s.execute(
        select(RelationshipRow).where(RelationshipRow.id == rel_id).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        raise GraphEditError(f"relationship {rel_id} not found")
    return row


async def _locked_triple(s: AsyncSession, subject_id: int, object_id: int,
                         predicate: str) -> RelationshipRow | None:
    return (await s.execute(
        select(RelationshipRow).where(
            RelationshipRow.subject_entity_id == subject_id,
            RelationshipRow.object_entity_id == object_id,
            RelationshipRow.predicate == predicate).with_for_update()
    )).scalar_one_or_none()


async def set_relationship(
    s: AsyncSession, subject_id: int, object_id: int, predicate: str,
    fact: str | None, confidence: float,
) -> tuple[int, dict[str, Any] | None, dict[str, Any]]:
    """(rel_id, до или None если новая, после). Существующую тройку обновляет
    и делает текущей, иначе заводит."""
    if predicate not in PREDICATES:
        raise GraphEditError(f"unknown predicate '{predicate}'; one of {PREDICATES}")
    if subject_id == object_id:
        raise GraphEditError("subject and object must differ")
    await _entity(s, subject_id)
    await _entity(s, object_id)
    now = utc_naive_now()
    row = await _locked_triple(s, subject_id, object_id, predicate)
    before = relationship_snapshot(row) if row is not None else None
    if row is None:
        # Параллельный вызов с той же тройкой не должен упасть на уникальном
        # индексе uq_relationships_spo: проигравший перечитывает победителя.
        created = await insert_relationship_if_absent(
            s, subject_id=subject_id, object_id=object_id, predicate=predicate,
            fact=fact, confidence=confidence, now=now)
        if created is not None:
            row = await _relationship(s, created)
            return row.id, None, relationship_snapshot(row)
        row = await _locked_triple(s, subject_id, object_id, predicate)
        before = relationship_snapshot(row) if row is not None else None
    row.fact, row.confidence = fact, confidence
    row.is_current, row.last_seen_at = True, now
    await s.flush()
    return row.id, before, relationship_snapshot(row)


async def retire_relationship(
    s: AsyncSession, rel_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = await _relationship(s, rel_id)
    before = relationship_snapshot(row)
    row.is_current = False
    await s.flush()
    return before, relationship_snapshot(row)


async def restore_relationship(s: AsyncSession, rel_id: int,
                               values: dict[str, Any]) -> dict[str, Any]:
    row = await _relationship(s, rel_id)
    row.fact, row.confidence = values["fact"], values["confidence"]
    row.is_current = values["is_current"]
    await s.flush()
    return relationship_snapshot(row)


async def current_name(s: AsyncSession, entity_id: int) -> dict[str, Any]:
    return {"name": (await _entity(s, entity_id)).name}


async def current_relationship(s: AsyncSession, rel_id: int) -> dict[str, Any]:
    return relationship_snapshot(await _relationship(s, rel_id))

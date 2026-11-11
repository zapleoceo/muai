"""SQL слияния двух сущностей. Все шаги принимают одну сессию: `merge_entities`
обязан пройти одной транзакцией, иначе обрыв посередине оставит полу-перенос."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_graph import EntityRow


async def load_attribute_pair(s: AsyncSession, keeper_id: int,
                              merged_id: int) -> tuple[Any, Any]:
    keeper_attrs, merged_attrs = (await s.execute(text(
        "SELECT (SELECT attributes FROM entities WHERE id = :keeper), "
        "       (SELECT attributes FROM entities WHERE id = :merged)"
    ), {"keeper": keeper_id, "merged": merged_id})).one()
    return keeper_attrs, merged_attrs


async def write_attributes(s: AsyncSession, entity_id: int, attributes: dict) -> None:
    await s.execute(
        update(EntityRow).where(EntityRow.id == entity_id).values(attributes=attributes)
    )


async def move_aliases(s: AsyncSession, keeper_id: int, merged_id: int) -> tuple[int, int]:
    """Перенести алиасы, конфликтующие удалить. Возвращает (перенесено, удалено)."""
    # rowcount, не CTE — data-modifying CTE не работает в SQLite, на которой идут тесты
    moved = (await s.execute(text("""
        UPDATE entity_aliases
        SET entity_id = :keeper
        WHERE entity_id = :merged
          AND NOT EXISTS (
            SELECT 1 FROM entity_aliases ea2
            WHERE ea2.entity_id = :keeper
              AND ea2.source = entity_aliases.source
              AND ea2.identifier = entity_aliases.identifier
          )
    """), {"keeper": keeper_id, "merged": merged_id})).rowcount or 0
    dropped = (await s.execute(text(
        "DELETE FROM entity_aliases WHERE entity_id = :merged RETURNING id"
    ), {"merged": merged_id})).rowcount
    return moved, dropped


async def move_memberships(s: AsyncSession, keeper_id: int, merged_id: int) -> int:
    """Обе стороны членств (сущность может быть и группой). UNIQUE — (parent, child,
    source); возвращает число перенесённых членств со стороны child."""
    params = {"keeper": keeper_id, "merged": merged_id}
    moved = (await s.execute(text(
        "UPDATE memberships SET child_entity_id = :keeper "
        "WHERE child_entity_id = :merged "
        "  AND NOT EXISTS (SELECT 1 FROM memberships m2 "
        "                  WHERE m2.child_entity_id = :keeper "
        "                    AND m2.parent_entity_id = memberships.parent_entity_id "
        "                    AND m2.source = memberships.source)"
    ), params)).rowcount
    await s.execute(text(
        "DELETE FROM memberships WHERE child_entity_id = :merged"
    ), {"merged": merged_id})
    await s.execute(text(
        "UPDATE memberships SET parent_entity_id = :keeper "
        "WHERE parent_entity_id = :merged "
        "  AND NOT EXISTS (SELECT 1 FROM memberships m2 "
        "                  WHERE m2.parent_entity_id = :keeper "
        "                    AND m2.child_entity_id = memberships.child_entity_id "
        "                    AND m2.source = memberships.source)"
    ), params)
    await s.execute(text(
        "DELETE FROM memberships WHERE parent_entity_id = :merged"
    ), {"merged": merged_id})
    return moved


async def move_relationships(s: AsyncSession, keeper_id: int, merged_id: int) -> None:
    # Миграция 017 добавила UNIQUE (subject, predicate, object) — неохраняемый
    # UPDATE ронял бы весь merge IntegrityError'ом. Связь merged↔keeper стала бы
    # самопетлёй keeper→keeper — удаляем, а не переносим.
    params = {"keeper": keeper_id, "merged": merged_id}
    await s.execute(text(
        "DELETE FROM relationships "
        "WHERE (subject_entity_id = :merged AND object_entity_id = :keeper) "
        "   OR (subject_entity_id = :keeper AND object_entity_id = :merged)"
    ), params)
    await s.execute(text(
        "UPDATE relationships SET subject_entity_id = :keeper "
        "WHERE subject_entity_id = :merged "
        "  AND NOT EXISTS (SELECT 1 FROM relationships r2 "
        "                  WHERE r2.subject_entity_id = :keeper "
        "                    AND r2.predicate = relationships.predicate "
        "                    AND r2.object_entity_id = relationships.object_entity_id)"
    ), params)
    await s.execute(text(
        "UPDATE relationships SET object_entity_id = :keeper "
        "WHERE object_entity_id = :merged "
        "  AND NOT EXISTS (SELECT 1 FROM relationships r2 "
        "                  WHERE r2.subject_entity_id = relationships.subject_entity_id "
        "                    AND r2.predicate = relationships.predicate "
        "                    AND r2.object_entity_id = :keeper)"
    ), params)
    # Остатки — дубли уже существующих у keeper'а связей
    await s.execute(text(
        "DELETE FROM relationships "
        "WHERE subject_entity_id = :merged OR object_entity_id = :merged"
    ), {"merged": merged_id})


async def delete_entity_row(s: AsyncSession, entity_id: int) -> None:
    await s.execute(text("DELETE FROM entities WHERE id = :merged"), {"merged": entity_id})

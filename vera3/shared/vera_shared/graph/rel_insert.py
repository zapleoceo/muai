"""Вставка связи одним `INSERT … ON CONFLICT DO NOTHING` — общая для всех писателей.

Проверка «есть ли такая тройка» перед вставкой гонится: две реплики triage
видят «нет» и обе вставляют, проигравшая падает `IntegrityError` на
`uq_relationships_spo`. Здесь конфликт решает сама база, а проигравший получает
None и перечитывает победителя.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_graph import RelationshipRow


async def insert_relationship_if_absent(
    s: AsyncSession, *, subject_id: int, object_id: int, predicate: str,
    fact: str | None, confidence: float, now: datetime,
    derived_from_event_id: int | None = None,
) -> int | None:
    """id новой строки или None, если тройка уже есть (в том числе погашенная)."""
    insert = pg_insert if s.get_bind().dialect.name == "postgresql" else sqlite_insert
    return (await s.execute(
        insert(RelationshipRow).values(
            subject_entity_id=subject_id, object_entity_id=object_id,
            predicate=predicate, fact=fact, confidence=confidence,
            derived_from_event_id=derived_from_event_id,
            first_seen_at=now, last_seen_at=now, is_current=True)
        .on_conflict_do_nothing(index_elements=["subject_entity_id", "predicate",
                                                "object_entity_id"])
        .returning(RelationshipRow.id)
    )).scalar_one_or_none()

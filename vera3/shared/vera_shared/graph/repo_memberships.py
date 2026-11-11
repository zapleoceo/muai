"""Членства (кто в какой группе) и групповые чаты проектов."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityRow, MembershipRow
from vera_shared.timeutil import utc_naive_now


async def upsert_membership(
    *, parent_entity_id: int, child_entity_id: int,
    source: str, role: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Upsert membership. Touches last_seen_at."""
    now = utc_naive_now()
    async with get_session() as s:
        existing = (await s.execute(
            select(MembershipRow).where(
                MembershipRow.parent_entity_id == parent_entity_id,
                MembershipRow.child_entity_id == child_entity_id,
                MembershipRow.source == source,
            )
        )).scalar_one_or_none()
        if existing:
            existing.last_seen_at = now
            existing.is_current = True
            if role:
                existing.role = role
            if attributes:
                existing.attributes = {**(existing.attributes or {}), **attributes}
            return
        s.add(MembershipRow(
            parent_entity_id=parent_entity_id,
            child_entity_id=child_entity_id,
            source=source, role=role,
            attributes=attributes or {},
            first_seen_at=now, last_seen_at=now, is_current=True,
        ))


async def list_members(parent_entity_id: int) -> list[dict[str, Any]]:
    async with get_session() as s:
        rs = await s.execute(
            select(MembershipRow, EntityRow)
            .join(EntityRow, EntityRow.id == MembershipRow.child_entity_id)
            .where(
                MembershipRow.parent_entity_id == parent_entity_id,
                MembershipRow.is_current.is_(True),
            )
        )
        return [
            {"entity_id": e.id, "name": e.name, "type": e.type,
             "role": m.role, "source": m.source,
             "attributes": {**e.attributes, **m.attributes}}
            for m, e in rs
        ]


async def find_project_chats() -> list[dict[str, Any]]:
    """Групповые чаты проектов: entity_id + tg_id + тип.

    Источник — `project_membership` (kind='chat'), сматченный на граф через
    alias `telegram:chat:<key>`. Жила в `ingestor_telegram/roster_sync.py`:
    воркер ингестора джойнил `entity_aliases` и `entities` напрямую.
    """
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT DISTINCT e.id AS entity_id, e.name, e.type,
                   (e.attributes->>'tg_id') AS tg_id
            FROM project_membership pm
            JOIN entity_aliases a
              ON a.source = 'telegram' AND a.identifier = 'chat:' || pm.key
            JOIN entities e ON e.id = a.entity_id
            WHERE pm.kind = 'chat'
              AND e.type IN ('group', 'supergroup')
        """))).mappings().all()
    return [dict(r) for r in rows]

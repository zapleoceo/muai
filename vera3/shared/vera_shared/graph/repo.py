"""Graph repository — sync/upsert API for entities/aliases/memberships/etc.

This is the ONLY layer that touches graph tables directly. Future swap to
Neo4j is a new implementation behind the same interface (DIP).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import (
    EntityAliasRow, EntityRow, IdentityNodeRow, MembershipRow, RelationshipRow,
)

log = logging.getLogger(__name__)


# ─── Entities & Aliases (Identity Resolution) ────────────────────────────────


async def upsert_entity(
    *, type: str, name: str,
    source: str, identifier: str,
    canonical_id: str | None = None,
    display_name: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> int:
    """Upsert (Entity + Alias). Returns entity_id.

    If an alias (source, identifier) already exists → return its entity.
    Otherwise create new Entity + Alias.
    """
    async with get_session() as s:
        alias = (await s.execute(
            select(EntityAliasRow).where(
                EntityAliasRow.source == source,
                EntityAliasRow.identifier == identifier,
            )
        )).scalar_one_or_none()

        if alias:
            # touch last_seen on entity
            await s.execute(
                update(EntityRow).where(EntityRow.id == alias.entity_id)
                .values(last_seen_at=datetime.utcnow())
            )
            if attributes:
                ent = (await s.execute(
                    select(EntityRow).where(EntityRow.id == alias.entity_id)
                )).scalar_one()
                ent.attributes = {**(ent.attributes or {}), **attributes}
            return alias.entity_id

        ent = EntityRow(
            type=type, name=name,
            canonical_id=canonical_id,
            attributes=attributes or {},
        )
        s.add(ent)
        await s.flush()
        s.add(EntityAliasRow(
            entity_id=ent.id, source=source, identifier=identifier,
            display_name=display_name or name, confidence=1.0,
        ))
        return ent.id


async def find_entity_by_name(name: str, type: str | None = None) -> int | None:
    """Fuzzy lookup. Useful for `tools.search_entities`."""
    async with get_session() as s:
        q = select(EntityRow.id).where(EntityRow.name.ilike(f"%{name}%"))
        if type:
            q = q.where(EntityRow.type == type)
        row = (await s.execute(q.limit(1))).scalar_one_or_none()
        return row


async def find_entity_by_alias(source: str, identifier: str) -> int | None:
    async with get_session() as s:
        return (await s.execute(
            select(EntityAliasRow.entity_id).where(
                EntityAliasRow.source == source,
                EntityAliasRow.identifier == identifier,
            )
        )).scalar_one_or_none()


# ─── Memberships ─────────────────────────────────────────────────────────────


async def upsert_membership(
    *, parent_entity_id: int, child_entity_id: int,
    source: str, role: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Upsert membership. Touches last_seen_at."""
    now = datetime.utcnow()
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


# ─── Relationships (Graphiti-style facts) ────────────────────────────────────


async def upsert_relationship(
    *, subject_entity_id: int, object_entity_id: int,
    predicate: str, fact: str | None = None,
    confidence: float = 0.6,
    derived_from_event_id: int | None = None,
) -> None:
    """Soft-upsert: if (subject, predicate, object) exists → touch last_seen.
    Otherwise insert."""
    now = datetime.utcnow()
    async with get_session() as s:
        existing = (await s.execute(
            select(RelationshipRow).where(
                RelationshipRow.subject_entity_id == subject_entity_id,
                RelationshipRow.object_entity_id == object_entity_id,
                RelationshipRow.predicate == predicate,
            )
        )).scalar_one_or_none()
        if existing:
            existing.last_seen_at = now
            existing.confidence = max(existing.confidence, confidence)
            if fact and not existing.fact:
                existing.fact = fact
            return
        s.add(RelationshipRow(
            subject_entity_id=subject_entity_id,
            object_entity_id=object_entity_id,
            predicate=predicate, fact=fact,
            confidence=confidence,
            derived_from_event_id=derived_from_event_id,
            first_seen_at=now, last_seen_at=now, is_current=True,
        ))


# ─── L3 Identity nodes (Goal/Value/NoGo/Style/Self/Fact) ─────────────────────


async def upsert_identity_node(
    *, type: str, label: str, payload: dict[str, Any],
    listener_entity_id: int | None = None,
    derived_from: dict[str, Any] | None = None,
    weight: float = 1.0, confidence: float = 0.7,
) -> int:
    """Upsert by (type, label, listener_entity_id). Style nodes are
    keyed by listener; Value/Goal by label only."""
    async with get_session() as s:
        q = select(IdentityNodeRow).where(
            IdentityNodeRow.type == type,
            IdentityNodeRow.label == label,
            IdentityNodeRow.listener_entity_id == listener_entity_id,
        )
        existing = (await s.execute(q)).scalar_one_or_none()
        if existing:
            existing.payload = payload
            existing.weight = weight
            existing.confidence = confidence
            if derived_from:
                existing.derived_from = derived_from
            existing.updated_at = datetime.utcnow()
            return existing.id
        node = IdentityNodeRow(
            type=type, label=label, payload=payload,
            listener_entity_id=listener_entity_id,
            derived_from=derived_from or {},
            weight=weight, confidence=confidence,
            is_current=True,
        )
        s.add(node)
        await s.flush()
        return node.id


async def get_style_for_listener(listener_entity_id: int) -> dict[str, Any] | None:
    async with get_session() as s:
        row = (await s.execute(
            select(IdentityNodeRow).where(
                IdentityNodeRow.type == "style",
                IdentityNodeRow.listener_entity_id == listener_entity_id,
                IdentityNodeRow.is_current.is_(True),
            )
        )).scalar_one_or_none()
        return row.payload if row else None


async def get_global_style() -> dict[str, Any] | None:
    """Fallback style profile when no per-listener exists."""
    async with get_session() as s:
        row = (await s.execute(
            select(IdentityNodeRow).where(
                IdentityNodeRow.type == "style",
                IdentityNodeRow.listener_entity_id.is_(None),
                IdentityNodeRow.is_current.is_(True),
            )
        )).scalar_one_or_none()
        return row.payload if row else None

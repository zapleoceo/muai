"""L3 identity nodes (Goal/Value/NoGo/Style/Self/Fact)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import IdentityNodeRow
from vera_shared.timeutil import utc_naive_now


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
            existing.updated_at = utc_naive_now()
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

"""SQL for identity resolution: candidate inputs and `merge_suggestions` rows."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session


async def load_identity_inputs() -> tuple[list[Any], list[Any], set[tuple[int, int]]]:
    """(person rows, membership rows, already-judged pairs) for candidate search."""
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT e.id, e.name, e.attributes,
                   (SELECT a.source FROM entity_aliases a
                     WHERE a.entity_id = e.id LIMIT 1) AS src
            FROM entities e WHERE e.type = 'person'
        """))).all()
        member_rows = (await s.execute(text(
            "SELECT child_entity_id, parent_entity_id FROM memberships"
        ))).all()
        judged = {tuple(r) for r in (await s.execute(text(
            "SELECT entity_a, entity_b FROM merge_suggestions"
        ))).all()}
    return list(rows), list(member_rows), judged


async def save_suggestion(a_id: int, b_id: int, verdict: dict[str, Any]) -> None:
    """Insert the judged pair. 'different' lands pre-rejected so it is never
    re-asked but also never shown as a pending suggestion."""
    a_id, b_id = sorted((a_id, b_id))
    status = "rejected" if verdict["verdict"] == "different" else "pending"
    async with get_session() as s:
        exists = (await s.execute(text(
            "SELECT 1 FROM merge_suggestions WHERE entity_a=:a AND entity_b=:b"
        ), {"a": a_id, "b": b_id})).first()
        if exists:
            return
        await s.execute(text(
            "INSERT INTO merge_suggestions "
            "(entity_a, entity_b, verdict, confidence, reason, status) "
            "VALUES (:a, :b, :v, :c, :r, :s)"
        ), {"a": a_id, "b": b_id, "v": verdict["verdict"],
            "c": verdict["confidence"], "r": verdict["reason"], "s": status})


async def list_pending_suggestions() -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT m.id, m.entity_a, m.entity_b, m.verdict, m.confidence, m.reason
            FROM merge_suggestions m
            WHERE m.status = 'pending'
            ORDER BY m.confidence DESC, m.id
        """))).mappings().all()
    return [dict(r) for r in rows]


async def set_suggestion_status(suggestion_id: int, status: str) -> dict | None:
    """Mark accepted/rejected; returns the row (for the merge step) or None."""
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT id, entity_a, entity_b FROM merge_suggestions WHERE id=:i"
        ), {"i": suggestion_id})).mappings().first()
        if row is None:
            return None
        await s.execute(text(
            "UPDATE merge_suggestions SET status=:s WHERE id=:i"
        ), {"s": status, "i": suggestion_id})
    return dict(row)

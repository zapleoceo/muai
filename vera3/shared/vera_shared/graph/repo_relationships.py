"""Чтение и пометка связей для аудита графа (`scripts/quarantine_junk_rels.py`).

Пометка — `is_current = false`, а не DELETE: мусорная связь перестаёт
показываться (`list_relationships`, `graph_snapshot`, степень в аватарах
фильтруют `is_current`), но остаётся в таблице с фактом и событием-источником,
и пометку можно снять по сохранённому списку id.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import and_, bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityAliasRow, EntityRow, RelationshipRow
from vera_shared.graph.rel_canon import (
    Triple,
    canonical_edge,
    equivalent_forms,
    rival_forms,
    strength,
)
from vera_shared.graph.rel_insert import insert_relationship_if_absent
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def list_current_relationships_page(after_id: int, limit: int) -> list[dict[str, Any]]:
    """Текущие связи с именами и типами обоих концов, keyset по id."""
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT r.id, r.predicate, r.confidence, r.fact,
                   es.name AS subject_name, es.type AS subject_type,
                   eo.name AS object_name, eo.type AS object_type
            FROM relationships r
            JOIN entities es ON es.id = r.subject_entity_id
            JOIN entities eo ON eo.id = r.object_entity_id
            WHERE r.is_current AND r.id > :after
            ORDER BY r.id
            LIMIT :lim
        """), {"after": after_id, "lim": limit})).mappings().all()
    return [dict(r) for r in rows]


async def set_relationships_current(ids: list[int], *, current: bool) -> int:
    """Проставить `is_current` пачке связей. Возвращает число изменённых строк."""
    if not ids:
        return 0
    async with get_session() as s:
        res = await s.execute(
            text("UPDATE relationships SET is_current = :cur WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"cur": current, "ids": ids},
        )
    return res.rowcount or 0


async def _row_by_form(s: AsyncSession, forms: list[Triple], *,
                       only_current: bool = False) -> RelationshipRow | None:
    cond = or_(*(and_(RelationshipRow.subject_entity_id == a,
                      RelationshipRow.predicate == p,
                      RelationshipRow.object_entity_id == b) for a, p, b in forms))
    q = select(RelationshipRow).where(cond).order_by(RelationshipRow.id).limit(1)
    if only_current:
        q = q.where(RelationshipRow.is_current.is_(True))
    return (await s.execute(q)).scalar_one_or_none()


async def _yields_to_rival(s: AsyncSession, subject_id: int, predicate: str,
                           object_id: int, newcomer: dict[str, Any]) -> bool:
    """Противоречие («A над B» при текущем «B над A»): остаётся более весомая.

    True — новая уступает и не пишется; иначе текущий соперник гасится.
    При равенстве остаётся уже записанная: связь не должна качаться туда-сюда."""
    rivals = rival_forms(subject_id, predicate, object_id)
    rival = await _row_by_form(s, rivals, only_current=True) if rivals else None
    if rival is None:
        return False
    if strength(newcomer) <= strength({"derived_from_event_id": rival.derived_from_event_id,
                                       "confidence": rival.confidence, "fact": rival.fact}):
        log.warning("связь %s→%s %s отклонена: противоречит текущей #%s",
                    subject_id, object_id, predicate, rival.id)
        return True
    rival.is_current = False
    log.warning("связь #%s погашена: новая %s→%s %s весомее", rival.id,
                subject_id, object_id, predicate)
    return False


async def upsert_relationship(
    *, subject_entity_id: int, object_entity_id: int,
    predicate: str, fact: str | None = None,
    confidence: float = 0.6,
    derived_from_event_id: int | None = None,
) -> bool:
    """Мягкий upsert в канонической форме (`rel_canon`): повтор той же связи —
    только `last_seen` и уверенность, False. Новая строка — True (rel-extract
    считает по нему живые связи). Противоречащая более слабая — False, не пишется."""
    s_id, pred, o_id = canonical_edge(subject_entity_id, predicate, object_entity_id)
    forms = equivalent_forms(subject_entity_id, predicate, object_entity_id)
    now = utc_naive_now()
    async with get_session() as s:
        existing = await _row_by_form(s, forms)
        if existing is None:
            newcomer = {"derived_from_event_id": derived_from_event_id,
                        "confidence": confidence, "fact": fact}
            if await _yields_to_rival(s, s_id, pred, o_id, newcomer):
                return False
            created = await insert_relationship_if_absent(
                s, subject_id=s_id, object_id=o_id, predicate=pred, fact=fact,
                confidence=confidence, now=now,
                derived_from_event_id=derived_from_event_id)
            if created is not None:
                return True
            existing = await _row_by_form(s, forms)
        if existing is not None:
            existing.last_seen_at = now
            existing.confidence = max(existing.confidence, confidence)
            if fact and not existing.fact:
                existing.fact = fact
        return False


async def resolve_strong_identifier(name: str) -> int | None:
    """Сущность по e-mail или @username из текста — однозначно, иначе None.

    Это «сильный» способ назвать человека: в отличие от имени, адрес и логин не
    повторяются у тёзок."""
    raw = name.strip()
    async with get_session() as s:
        if _EMAIL_RE.match(raw):
            ids = (await s.execute(
                select(EntityAliasRow.entity_id).distinct().where(
                    EntityAliasRow.source == "gmail",
                    EntityAliasRow.identifier == raw.lower()).limit(2))).scalars().all()
        elif raw.startswith("@") and len(raw) > 2:
            ids = (await s.execute(
                select(EntityRow.id).where(
                    text("lower(attributes->>'username') = :u")).params(u=raw[1:].lower())
                .limit(2))).scalars().all()
        else:
            return None
    return ids[0] if len(ids) == 1 else None


async def entity_names(entity_ids: list[int]) -> dict[int, list[str]]:
    """Имя сущности и display-имена её алиасов — чем её могут назвать в факте."""
    if not entity_ids:
        return {}
    names: dict[int, list[str]] = {i: [] for i in entity_ids}
    async with get_session() as s:
        for eid, name in (await s.execute(
                select(EntityRow.id, EntityRow.name).where(EntityRow.id.in_(entity_ids)))):
            names[eid].append(name)
        for eid, display in (await s.execute(
                select(EntityAliasRow.entity_id, EntityAliasRow.display_name).where(
                    EntityAliasRow.entity_id.in_(entity_ids),
                    EntityAliasRow.display_name.is_not(None)))):
            names[eid].append(display)
    return names

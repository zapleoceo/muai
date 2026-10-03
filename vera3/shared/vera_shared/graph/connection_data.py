"""Чтение данных для связей пар: записи ролей, карточки сущностей, рабочие идентификаторы.

Только SELECT'ы; сборка связи — `connection_model`, выдача — `connections`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session
from vera_shared.graph.connection_model import Claim
from vera_shared.graph.dupe_keys import org_domain

_CLAIM_COLUMNS = ("r.id, r.subject_entity_id AS s, r.predicate, r.object_entity_id AS o, "
                  "r.confidence, r.fact, r.last_seen_at, r.derived_from_event_id")


class Card(NamedTuple):
    name: str
    type: str


@dataclass(frozen=True)
class WorkIdent:
    """Чем человек привязан к работе: корпоративные домены почты и Slack."""
    domains: frozenset[str] = frozenset()
    slack: bool = False


def shared_work(a: WorkIdent | None, b: WorkIdent | None) -> bool:
    """Общий корпоративный домен или оба в Slack-пространстве владельца."""
    if a is None or b is None:
        return False
    return bool(a.domains & b.domains) or (a.slack and b.slack)


def _when(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _claim(row: Any) -> Claim:
    return Claim(
        subject_id=row["s"], predicate=row["predicate"], object_id=row["o"],
        confidence=float(row["confidence"] or 0.0),
        manual=row["derived_from_event_id"] is None, fact=row["fact"],
        seen_at=_when(row["last_seen_at"]), rel_id=row["id"],
        event_id=row["derived_from_event_id"])


async def claims_of(entity_id: int) -> list[Claim]:
    """Текущие записи ролей сущности в обе стороны."""
    async with get_session() as s:
        rows = (await s.execute(text(
            f"SELECT {_CLAIM_COLUMNS} FROM relationships r WHERE r.is_current "
            "AND (r.subject_entity_id = :e OR r.object_entity_id = :e)"),
            {"e": entity_id})).mappings().all()
    return [_claim(r) for r in rows]


async def claims_within(ids: list[int], predicate: str | None = None) -> list[Claim]:
    """Текущие записи, у которых ОБА конца в `ids` (опционально — один предикат)."""
    if not ids:
        return []
    where = "AND r.predicate = :pred" if predicate else ""
    async with get_session() as s:
        rows = (await s.execute(text(
            f"SELECT {_CLAIM_COLUMNS} FROM relationships r WHERE r.is_current {where} "
            "AND r.subject_entity_id IN :ids AND r.object_entity_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": ids, **({"pred": predicate} if predicate else {})})).mappings().all()
    return [_claim(r) for r in rows]


async def relationship_triples(ids: list[int]) -> dict[int, tuple[int, str, int]]:
    """id записи → (субъект, предикат, объект); для журнала правок, чьи строки не хранят концов."""
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT id, subject_entity_id, predicate, object_entity_id FROM relationships "
                 "WHERE id IN :ids").bindparams(bindparam("ids", expanding=True)),
            {"ids": sorted(set(ids))})).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def entity_cards(ids: list[int]) -> dict[int, Card]:
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT id, name, type FROM entities WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)), {"ids": sorted(set(ids))})).all()
    return {r[0]: Card(r[1], r[2]) for r in rows}


async def work_idents(ids: list[int]) -> dict[int, WorkIdent]:
    """Домен из `attributes.email` и gmail-алиасов (free-mail не в счёт) + Slack-алиас."""
    if not ids:
        return {}
    unique = sorted(set(ids))
    domains: dict[int, set[str]] = {i: set() for i in unique}
    slack: set[int] = set()
    async with get_session() as s:
        emails = (await s.execute(
            text("SELECT id, attributes->>'email' FROM entities WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)), {"ids": unique})).all()
        aliases = (await s.execute(
            text("SELECT entity_id, source, identifier FROM entity_aliases "
                 "WHERE source IN ('gmail', 'slack') AND entity_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)), {"ids": unique})).all()
    for entity_id, email in emails:
        if domain := org_domain(email):
            domains[entity_id].add(domain)
    for entity_id, source, identifier in aliases:
        if source == "slack":
            slack.add(entity_id)
        elif domain := org_domain(identifier):
            domains[entity_id].add(domain)
    return {i: WorkIdent(frozenset(domains[i]), i in slack) for i in unique}

"""Срез графа для поиска дублей: сущности, их алиасы и «вес» (число связей).

Детектор работает по срезу, а не по БД: тот же код гоняется и на живой базе, и
на SELECT-выгрузке с прода (read-only), и в юнит-тестах без SQL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import (
    EntityAliasRow,
    EntityRow,
    MembershipRow,
    RelationshipRow,
)


@dataclass
class Ent:
    id: int
    type: str
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    aliases: list[tuple[str, str]] = field(default_factory=list)
    degree: int = 0

    def identifiers(self, source: str) -> list[str]:
        return [i for s, i in self.aliases if s == source]

    @property
    def tg_id(self) -> str | None:
        value = self.attributes.get("tg_id")
        return None if value is None else str(value)

    def brief(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "degree": self.degree,
                "aliases": [f"{s}:{i}" for s, i in self.aliases]}


@dataclass
class Snapshot:
    entities: dict[int, Ent]

    def of_type(self, *types: str) -> list[Ent]:
        return [e for e in self.entities.values() if e.type in types]


def snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    """Формат выгрузки: `entities[{id,type,name,attributes}]`,
    `aliases[{entity_id,source,identifier}]`, `degree{id: n}`."""
    ents = {int(e["id"]): Ent(int(e["id"]), e["type"], e["name"] or "",
                              e.get("attributes") or {})
            for e in data["entities"]}
    for a in data.get("aliases") or []:
        if int(a["entity_id"]) in ents:
            ents[int(a["entity_id"])].aliases.append((a["source"], a["identifier"]))
    for key, n in (data.get("degree") or {}).items():
        if int(key) in ents:
            ents[int(key)].degree = int(n)
    return Snapshot(ents)


async def _degrees(s: Any) -> dict[int, int]:
    degree: dict[int, int] = {}
    for col in (RelationshipRow.subject_entity_id, RelationshipRow.object_entity_id,
                MembershipRow.parent_entity_id, MembershipRow.child_entity_id):
        for eid, n in (await s.execute(select(col, func.count()).group_by(col))).all():
            degree[eid] = degree.get(eid, 0) + n
    return degree


async def load_snapshot() -> Snapshot:
    async with get_session() as s:
        entities = (await s.execute(select(EntityRow))).scalars().all()
        aliases = (await s.execute(select(EntityAliasRow))).scalars().all()
        degree = await _degrees(s)
        data = {
            "entities": [{"id": e.id, "type": e.type, "name": e.name,
                          "attributes": e.attributes} for e in entities],
            "aliases": [{"entity_id": a.entity_id, "source": a.source,
                         "identifier": a.identifier} for a in aliases],
            "degree": degree,
        }
    return snapshot_from_dict(data)

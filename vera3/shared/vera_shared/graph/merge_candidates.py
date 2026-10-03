"""Карточки-кандидаты для интерфейса объединения: кто это, сколько у него данных, кто останется.

`entity_summaries` отдаёт по сущности имя, @username, email, последнюю активность и
счётчики (алиасы, текущие связи, группы). `recommend_keep` выбирает главную карточку — ту,
где данных больше (при равенстве — более старую, её id меньше): остальное переедет в неё.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session


def _attrs(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return json.loads(raw or "{}")
    return dict(raw or {})


async def _counts(s: Any, sql: str, ids: list[int]) -> dict[int, int]:
    rows = (await s.execute(text(sql).bindparams(bindparam("ids", expanding=True)),
                            {"ids": ids})).all()
    return {r[0]: int(r[1]) for r in rows}


async def entity_summaries(ids: list[int]) -> dict[int, dict[str, Any]]:
    unique = sorted(set(ids))
    if not unique:
        return {}
    async with get_session() as s:
        people = (await s.execute(
            text("SELECT id, name, type, attributes, last_seen_at FROM entities WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)), {"ids": unique})).mappings().all()
        aliases = await _counts(s, "SELECT entity_id, count(*) FROM entity_aliases "
                                   "WHERE entity_id IN :ids GROUP BY entity_id", unique)
        groups = await _counts(s, "SELECT child_entity_id, count(*) FROM memberships "
                                  "WHERE is_current AND child_entity_id IN :ids "
                                  "GROUP BY child_entity_id", unique)
        rels = {i: 0 for i in unique}
        for column in ("subject_entity_id", "object_entity_id"):
            for key, n in (await _counts(
                    s, f"SELECT {column}, count(*) FROM relationships WHERE is_current "
                       f"AND {column} IN :ids GROUP BY {column}", unique)).items():
                rels[key] += n
    out = {}
    for r in people:
        attrs = _attrs(r["attributes"])
        out[r["id"]] = {
            "id": r["id"], "name": r["name"], "type": r["type"],
            "username": attrs.get("username") or None, "email": attrs.get("email") or None,
            "last_seen_at": str(r["last_seen_at"]) if r["last_seen_at"] else None,
            "aliases": aliases.get(r["id"], 0), "relationships": rels.get(r["id"], 0),
            "groups": groups.get(r["id"], 0)}
    return out


def data_weight(summary: dict[str, Any]) -> int:
    return summary["aliases"] + summary["relationships"] + summary["groups"]


def recommend_keep(a: dict[str, Any], b: dict[str, Any]) -> int:
    """id карточки, которая должна остаться главной."""
    if data_weight(a) != data_weight(b):
        return a["id"] if data_weight(a) > data_weight(b) else b["id"]
    return min(a["id"], b["id"])

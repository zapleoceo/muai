"""План чистки связей, привязанных к тёзке вне круга разговора.

До появления круга одиночное имя резолвилось в «единственного человека с таким именем»,
а им оказывался чужой: «Дима» из переписки владельца уезжал к случайному Диме,
написавшему десять сообщений в публичном чате. Здесь для каждой извлечённой связи с
концом-одиночным именем берётся круг её события-источника: конец в круге — остаётся;
вне круга и в круге ровно один подходящий — переставить конец на него (`repoint`);
иначе погасить (`retire`). Ручные связи (без события) и связи без события в базе не
трогаются. План — в формате `rel_cleanup` (применение и откат — `rel_cleanup_apply`).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session
from vera_shared.graph.rel_canon import canonical_edge
from vera_shared.graph.rel_cleanup import Action, plan_document, retire_action
from vera_shared.graph.rel_text import single_token_name
from vera_shared.links.circle import EventCircle, event_circle, resolve_short_name

RULE_REPOINT, RULE_OUT_OF_CIRCLE = "namesake_repoint", "namesake_retire"
_ROWS = """
SELECT r.id, r.subject_entity_id, r.predicate, r.object_entity_id, r.confidence, r.fact,
       r.is_current, r.derived_from_event_id,
       es.name AS subject_name, es.type AS subject_type,
       eo.name AS object_name, eo.type AS object_type
FROM relationships r
JOIN entities es ON es.id = r.subject_entity_id
JOIN entities eo ON eo.id = r.object_entity_id
WHERE r.is_current AND r.derived_from_event_id IS NOT NULL
  AND (es.type = 'person' OR eo.type = 'person')
{scope}
ORDER BY r.id"""


async def load_rows(entity_ids: Iterable[int] | None = None) -> list[dict[str, Any]]:
    """Текущие извлечённые связи (всех людей или только перечисленных концов)."""
    if entity_ids is None:
        stmt, params = text(_ROWS.format(scope="")), {}
    else:
        stmt = text(_ROWS.format(scope="AND (r.subject_entity_id IN :ids OR r.object_entity_id IN :ids)")
                    ).bindparams(bindparam("ids", expanding=True))
        params = {"ids": sorted(set(entity_ids))}
    async with get_session() as s:
        return [dict(r) for r in (await s.execute(stmt, params)).mappings()]


def _state(row: dict[str, Any]) -> dict[str, Any]:
    return {"subject_entity_id": row["subject_entity_id"], "predicate": row["predicate"],
            "object_entity_id": row["object_entity_id"], "is_current": row["is_current"]}


def _repoint(row: dict[str, Any], old: int, new: int) -> Action:
    s, o = row["subject_entity_id"], row["object_entity_id"]
    s, o = (new if s == old else s), (new if o == old else o)
    s, p, o = canonical_edge(s, row["predicate"], o)
    brief = f"{row['subject_name']} -[{row['predicate']}]-> {row['object_name']} | заменить конец {old} на {new}"
    return {"action": "convert", "rule": RULE_REPOINT, "rel_id": row["id"], "keep_id": None,
            "brief": brief, "before": _state(row),
            "after": {"subject_entity_id": s, "predicate": p, "object_entity_id": o,
                      "is_current": True}}


async def _judge_end(row: dict[str, Any], side: str, circle: EventCircle) -> Action | None:
    entity_id = row[f"{side}_entity_id"]
    if row[f"{side}_type"] != "person" or not single_token_name(row[f"{side}_name"]):
        return None
    if entity_id in circle.primary or entity_id in circle.extended:
        return None
    found = await resolve_short_name(row[f"{side}_name"], circle)
    other = row["object_entity_id" if side == "subject" else "subject_entity_id"]
    if found is None or found == other:
        return retire_action(row, RULE_OUT_OF_CIRCLE)
    return _repoint(row, entity_id, found)


async def build_namesake_plan(rows: list[dict[str, Any]]) -> list[Action]:
    """Действия по связям; круг события читается из БД (по одному запросу на событие)."""
    circles: dict[int, EventCircle | None] = {}
    actions: list[Action] = []
    for row in rows:
        event_id = row["derived_from_event_id"]
        if event_id not in circles:
            circles[event_id] = await event_circle(event_id)
        circle = circles[event_id]
        if circle is None:
            continue
        for side in ("subject", "object"):
            if action := await _judge_end(row, side, circle):
                actions.append(action)
                break
    for number, action in enumerate(actions, 1):
        action["id"] = number
    return actions


def namesake_document(actions: list[Action], scope: str) -> dict[str, Any]:
    doc = plan_document(actions, scope, "namesake")
    doc["by_rule"] = dict(Counter(a["rule"] for a in actions))
    return doc

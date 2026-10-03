"""План чистки уже записанных связей — чистая функция по срезу, без базы.

Те же правила, что стоят перед записью (`rel_validate`, `rel_canon`), но по
накопленному: связи, не прошедшие проверку, дубли симметричных, противоречия
«A над B» / «B над A» и обратные пары. Ничего не удаляется — план состоит из
действий `retire` (is_current=false) и `convert` (привести к канонической
форме), и оба обратимы по отчёту.

Связи без события-источника заведены руками (MCP, дашборд) — по извлечению их
не судят, но в нормализации они участвуют, и при конфликте ручная побеждает.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from vera_shared.graph.rel_canon import (
    ANTISYMMETRIC,
    SYMMETRIC,
    Triple,
    canonical_edge,
    strength,
)
from vera_shared.graph.rel_text import End, Evidence
from vera_shared.graph.rel_validate import relationship_reject_reason

PLAN_VERSION = 1
RULE_SYMMETRIC = "symmetric_duplicate"
RULE_INVERSE = "inverse_duplicate"
RULE_CONTRADICTION = "contradiction"
RULE_CONVERT = "convert_inverse"
RULE_BLOCKED = "convert_blocked"

Row = dict[str, Any]
Action = dict[str, Any]


def _triple(row: Row) -> Triple:
    return row["subject_entity_id"], row["predicate"], row["object_entity_id"]


def _rank(row: Row) -> tuple[Any, ...]:
    return (*strength(row), -row["id"])


def _brief(row: Row) -> str:
    return (f"{row['subject_name']} -[{row['predicate']}]-> {row['object_name']}"
            f" | {(row.get('fact') or '')[:80]}")


def _state(row: Row) -> dict[str, Any]:
    return {"subject_entity_id": row["subject_entity_id"], "predicate": row["predicate"],
            "object_entity_id": row["object_entity_id"], "is_current": row["is_current"]}


def _retire(row: Row, rule: str, keep: Row | None = None) -> Action:
    return {"action": "retire", "rule": rule, "rel_id": row["id"],
            "keep_id": keep["id"] if keep else None, "brief": _brief(row),
            "before": _state(row), "after": {**_state(row), "is_current": False}}


def _convert(row: Row) -> Action:
    s, p, o = canonical_edge(*_triple(row))
    return {"action": "convert", "rule": RULE_CONVERT, "rel_id": row["id"],
            "keep_id": None, "brief": _brief(row), "before": _state(row),
            "after": {"subject_entity_id": s, "predicate": p, "object_entity_id": o,
                      "is_current": True}}


def _reject_reason(row: Row, names: dict[str, list[str]], owner_id: int | None) -> str | None:
    def end(entity_id: int) -> End:
        own = entity_id == owner_id
        return End(tuple(names.get(str(entity_id), ())), strong=own, author=own)

    return relationship_reject_reason(
        subject_name=row["subject_name"], subject_type=row["subject_type"],
        predicate=row["predicate"], object_name=row["object_name"],
        object_type=row["object_type"], confidence=float(row["confidence"]),
        evidence=Evidence(row.get("fact"), end(row["subject_entity_id"]),
                          end(row["object_entity_id"])))


def build_plan(snapshot: dict[str, Any]) -> list[Action]:
    rows: list[Row] = snapshot["relationships"]
    names: dict[str, list[str]] = snapshot.get("names", {})
    owner_id: int | None = snapshot.get("owner_id")
    actions: list[Action] = []
    dead: set[int] = set()

    def drop(row: Row, rule: str, keep: Row | None = None) -> None:
        dead.add(row["id"])
        actions.append(_retire(row, rule, keep))

    current = [r for r in rows if r["is_current"]]
    for row in current:
        if row.get("derived_from_event_id") is None:
            continue
        if reason := _reject_reason(row, names, owner_id):
            drop(row, reason)

    groups: dict[Triple, list[Row]] = defaultdict(list)
    for row in current:
        if row["id"] not in dead:
            groups[canonical_edge(*_triple(row))].append(row)
    winners: dict[Triple, Row] = {}
    for canon, members in groups.items():
        members.sort(key=_rank, reverse=True)
        winners[canon] = members[0]
        for loser in members[1:]:
            drop(loser, RULE_SYMMETRIC if canon[1] in SYMMETRIC else RULE_INVERSE,
                 members[0])

    for (s, p, o), row in list(winners.items()):
        rival = winners.get((o, p, s))
        if p not in ANTISYMMETRIC or s > o or rival is None:
            continue
        loser, keep = sorted((row, rival), key=_rank)
        drop(loser, RULE_CONTRADICTION, keep)

    taken = {_triple(r) for r in rows}
    for row in current:
        if row["id"] in dead or _triple(row) == canonical_edge(*_triple(row)):
            continue
        if canonical_edge(*_triple(row)) in taken:
            actions.append({"action": "skip", "rule": RULE_BLOCKED, "rel_id": row["id"],
                            "keep_id": None, "brief": _brief(row)})
        else:
            actions.append(_convert(row))
    for number, action in enumerate(actions, 1):
        action["id"] = number
    return actions


def plan_document(actions: list[Action], source: str) -> dict[str, Any]:
    counts = Counter(a["rule"] for a in actions)
    return {"version": PLAN_VERSION, "generated_at": datetime.now(UTC).isoformat(),
            "source": source, "to_apply": sum(a["action"] != "skip" for a in actions),
            "counts": dict(counts), "actions": actions}

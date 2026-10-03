"""План чистки уже записанных связей — чистая функция по срезу, без базы.

Те же правила, что стоят перед записью (`rel_validate`, `rel_canon`), но по
накопленному: связи, не прошедшие проверку, дубли симметричных, противоречия
«A над B» / «B над A» и обратные пары. Ничего не удаляется — план состоит из
действий `retire` (is_current=false) и `convert` (привести к канонической
форме), и оба обратимы по отчёту.

Два прохода. `soft` — всё, что опровергается без модели: факт не называет концы,
тип концов, дубли, противоречия, обратные пары. Персона из одного слова
(`weak_name`) одним правилом НЕ гасится: среди таких связей много верных
(«Маша — дочь»), их разбирает `rel_cleanup_verify` через `rel_verify`.

Связи без события-источника заведены руками (MCP, дашборд) — по извлечению их
не судят, но в нормализации они участвуют, и при конфликте ручная побеждает.
"""
from __future__ import annotations

import json
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
from vera_shared.graph.rel_validate import REJECT_WEAK_NAME, relationship_reject_reason
from vera_shared.ingest.authorship import OWNER, resolve_author

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


def retire_action(row: Row, rule: str, keep: Row | None = None) -> Action:
    return {"action": "retire", "rule": rule, "rel_id": row["id"],
            "keep_id": keep["id"] if keep else None, "brief": _brief(row),
            "before": _state(row), "after": {**_state(row), "is_current": False}}


def _convert(row: Row) -> Action:
    s, p, o = canonical_edge(*_triple(row))
    return {"action": "convert", "rule": RULE_CONVERT, "rel_id": row["id"],
            "keep_id": None, "brief": _brief(row), "before": _state(row),
            "after": {"subject_entity_id": s, "predicate": p, "object_entity_id": o,
                      "is_current": True}}


def author_entity_id(row: Row, aliases: dict[str, int], owner_id: int | None) -> int | None:
    """Кто написал сообщение-источник связи: по тем же правилам, что при
    извлечении (`ingest.authorship`). «Я» в факте — это он, а не только владелец."""
    source = row.get("event_source")
    if not source:
        return None
    meta = row.get("event_meta") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    author = resolve_author(source, meta)
    if author is OWNER:
        return owner_id
    return aliases.get(f"{author[0]}:{author[1]}") if author else None


def _reject_reason(row: Row, snapshot: dict[str, Any]) -> str | None:
    names, owner_id = snapshot.get("names", {}), snapshot.get("owner_id")
    author_id = author_entity_id(row, snapshot.get("aliases", {}), owner_id)

    def end(entity_id: int) -> End:
        own = entity_id in (owner_id, author_id)
        return End(tuple(names.get(str(entity_id), ())), strong=own, author=own)

    return relationship_reject_reason(
        subject_name=row["subject_name"], subject_type=row["subject_type"],
        predicate=row["predicate"], object_name=row["object_name"],
        object_type=row["object_type"], confidence=float(row["confidence"]),
        evidence=Evidence(row.get("fact"), end(row["subject_entity_id"]),
                          end(row["object_entity_id"])))


def _extracted_reasons(snapshot: dict[str, Any]) -> dict[int, str]:
    return {r["id"]: reason for r in snapshot["relationships"]
            if r["is_current"] and r.get("derived_from_event_id") is not None
            and (reason := _reject_reason(r, snapshot))}


def soft_retirements(snapshot: dict[str, Any]) -> dict[int, str]:
    """id → причина для извлечённых связей, опровергаемых без модели. Связь с
    одиночным именем сюда не попадает по правилам «одно слово» и «факт не
    называет концы»: на ней настоящая связь легко выглядит как мусор («Маша —
    дочь» без имени владельца), её судит `rel_verify` по тексту сообщения."""
    return {i: r for i, r in _extracted_reasons(snapshot).items() if r != REJECT_WEAK_NAME}


def weak_name_candidates(snapshot: dict[str, Any]) -> list[Row]:
    """Связи с персоной из одного слова, которых не опровергло ничего другое."""
    weak = {i for i, r in _extracted_reasons(snapshot).items() if r == REJECT_WEAK_NAME}
    return [r for r in snapshot["relationships"] if r["id"] in weak]


def build_plan(snapshot: dict[str, Any]) -> list[Action]:
    """План прохода `soft`."""
    rows: list[Row] = snapshot["relationships"]
    actions: list[Action] = []
    # Связи с одиночным именем ждут вердикта: нормализовать их до него нельзя —
    # пара, из которой модель оставит одну, иначе потеряет не ту.
    dead: set[int] = {r["id"] for r in weak_name_candidates(snapshot)}

    def drop(row: Row, rule: str, keep: Row | None = None) -> None:
        dead.add(row["id"])
        actions.append(retire_action(row, rule, keep))

    current = [r for r in rows if r["is_current"]]
    soft = soft_retirements(snapshot)
    for row in current:
        if reason := soft.get(row["id"]):
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


def plan_document(actions: list[Action], source: str, phase: str = "soft") -> dict[str, Any]:
    counts = Counter(a["rule"] for a in actions)
    return {"version": PLAN_VERSION, "generated_at": datetime.now(UTC).isoformat(),
            "source": source, "phase": phase, "to_apply": sum(a["action"] != "skip" for a in actions),
            "counts": dict(counts), "actions": actions}

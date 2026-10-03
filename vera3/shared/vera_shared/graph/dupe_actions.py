"""Строители действий плана: единый формат для всех случаев детектора."""
from __future__ import annotations

from typing import Any

from vera_shared.graph.dupe_snapshot import Ent

Action = dict[str, Any]


def merge_action(case: int, keep: Ent, drops: list[Ent], reason: str) -> Action:
    return {"case": case, "action": "merge", "keep": keep.id,
            "drop": [d.id for d in drops], "reason": reason,
            "who": {e.id: e.brief() for e in (keep, *drops)}}


def skip_action(case: int, ents: list[Ent], reason: str) -> Action:
    return {"case": case, "action": "skip", "ids": [e.id for e in ents],
            "reason": reason, "who": {e.id: e.brief() for e in ents}}


def rename_action(case: int, e: Ent, new_name: str, reason: str) -> Action:
    return {"case": case, "action": "rename", "entity": e.id, "new_name": new_name,
            "reason": reason, "who": {e.id: e.brief()}}


def retype_action(case: int, e: Ent, new_type: str, reason: str) -> Action:
    return {"case": case, "action": "retype", "entity": e.id, "new_type": new_type,
            "reason": reason, "who": {e.id: e.brief()}}


def heaviest(ents: list[Ent]) -> Ent:
    return min(ents, key=lambda e: (-e.degree, e.id))

"""Candidate pairs for identity resolution: same canonical first name AND a
corroborating signal (different sources, shared chat, same last name)."""
from __future__ import annotations

from vera_shared.graph.dedup import _as_dict
from vera_shared.graph.identity_names import canonical_name_parts
from vera_shared.graph.identity_store import load_identity_inputs


async def find_identity_candidates(limit: int = 12) -> list[tuple[int, int, str]]:
    """(entity_a, entity_b, signal) pairs worth asking Vera about. Ordered:
    cross-source first (the gmail↔telegram unification case), then shared-chat,
    then same-last-name. Pairs already judged (any status) are excluded."""
    rows, member_rows, judged = await load_identity_inputs()

    chats_of: dict[int, set[int]] = {}
    for child, parent in member_rows:
        chats_of.setdefault(child, set()).add(parent)

    by_first: dict[str, list[dict]] = {}
    for eid, name, attrs, src in rows:
        first, last = canonical_name_parts(name)
        if len(first) < 3:
            continue
        by_first.setdefault(first, []).append(
            {"id": eid, "last": last, "src": src or "telegram",
             "attrs": _as_dict(attrs)})

    out: list[tuple[int, int, str, int]] = []   # (a, b, signal, rank)
    for group in by_first.values():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                pa, pb = sorted((a["id"], b["id"]))
                if (pa, pb) in judged:
                    continue
                if a["src"] != b["src"]:
                    out.append((pa, pb, "разные каналы (кросс-источник)", 0))
                elif chats_of.get(a["id"], set()) & chats_of.get(b["id"], set()):
                    out.append((pa, pb, "общие чаты", 1))
                elif a["last"] and a["last"] == b["last"]:
                    out.append((pa, pb, "совпадает фамилия", 2))
    out.sort(key=lambda t: t[3])
    return [(a, b, sig) for a, b, sig, _ in out[:limit]]

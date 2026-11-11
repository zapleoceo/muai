"""Поиск кандидатов в дубли: совпадение нормализованного имени и общий @username."""
from __future__ import annotations

import re
import unicodedata

from vera_shared.graph.dedup_store import _as_dict, load_entity_names


def normalize_name(s: str) -> str:
    """Case-fold, strip diacritics, collapse whitespace.

    'Алексей Самойлов'  → 'алексеи самоилов'
    'Alexey  Samoilov'  → 'alexey samoilov'
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).lower()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip()
    # ё→е, й→и are different codepoints — fold to base letter for fuzzy match
    return s.replace("ё", "е").replace("й", "и")


async def find_duplicates_by_name(min_group: int = 2) -> list[dict]:
    """Return groups of entity_ids sharing normalized name."""
    groups: dict[str, list[tuple[int, str]]] = {}
    for eid, name in await load_entity_names():
        key = normalize_name(name)
        if not key or len(key) < 2:
            continue
        groups.setdefault(key, []).append((eid, name))

    out = []
    for norm_name, members in groups.items():
        if len(members) >= min_group:
            out.append({
                "normalized": norm_name,
                "candidates": [{"id": eid, "name": n} for eid, n in members],
                "size": len(members),
            })
    out.sort(key=lambda g: -g["size"])
    return out


def group_alias_collisions(rows: list[dict], min_group: int = 2) -> list[dict]:
    """Groups of DIFFERENT entities sharing one lowercased @username.

    This is the high-precision "real duplicate" signal, distinct from
    `find_duplicates_by_name` (which only catches same first-name namesakes,
    almost always different people). A shared @username means the same
    Telegram object surfaced twice — typically a channel that posts under its
    own handle, creating both a `channel` and a `person` entity for one handle.
    Grouped in Python (not SQL JSON operators) so it stays portable to the
    SQLite test engine.
    """
    groups: dict[str, list[dict]] = {}
    for r in rows:
        attrs = _as_dict(r["attributes"])
        uname = (attrs.get("username") or "").strip().lower()
        if not uname:
            continue
        groups.setdefault(uname, []).append(
            {"id": r["id"], "name": r["name"], "type": r["type"]}
        )

    out = [
        {"username": uname, "candidates": members, "size": len(members)}
        for uname, members in groups.items()
        if len(members) >= min_group
    ]
    out.sort(key=lambda g: -g["size"])
    return out

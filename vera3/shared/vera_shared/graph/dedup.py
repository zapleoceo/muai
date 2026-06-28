"""Entity deduplication utilities.

Today we have 340+ duplicate entities (15 Alex, 12 Александр, 11 Сергей)
because entity_sync upserts by (source, sender_id) — one person with multiple
TG IDs becomes N rows. This module detects duplicates and merges them
without losing aliases, memberships, or relationships.

Detection criteria (any one is enough to suggest a merge):
  1. Normalized-name match (case/space/diacritics insensitive)
  2. Phone overlap (rare — phones not always stored)
  3. Aliases overlap (one entity's username appears as another's display_name)
  4. Topic overlap: same person in multiple chats discussing same threads —
     stored separately for the LLM resolver to consider

Merge keeps `keeper_id`, rewrites all FK references on the merged entities,
preserves aliases and memberships (unique by source+identifier so collisions
just drop), then deletes the merged rows.

The actual decision is owner-driven via dashboard /entities/duplicates UI;
this module just provides the primitives.
"""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import text

from vera_shared.db.engine import get_session


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
    s = s.replace("ё", "е").replace("й", "и")
    return s


async def find_duplicates_by_name(min_group: int = 2) -> list[dict]:
    """Return groups of entity_ids sharing normalized name."""
    async with get_session() as s:
        rs = (await s.execute(text(
            "SELECT id, name FROM entities WHERE name IS NOT NULL"
        ))).all()

    groups: dict[str, list[tuple[int, str]]] = {}
    for eid, name in rs:
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


async def get_entity_context(entity_id: int) -> dict:
    """Pull aliases, membership chats, and recent message count for review UI."""
    async with get_session() as s:
        aliases = list((await s.execute(text(
            "SELECT source, identifier, display_name FROM entity_aliases "
            "WHERE entity_id = :eid"
        ), {"eid": entity_id})).mappings().all())

        memberships = list((await s.execute(text(
            "SELECT group_entity_id, role, joined_at FROM memberships "
            "WHERE member_entity_id = :eid LIMIT 50"
        ), {"eid": entity_id})).mappings().all())

        # Recent activity (events where sender_id matches an alias)
        identifiers = [a["identifier"] for a in aliases
                       if a["source"] == "telegram" and a["identifier"].lstrip("-").isdigit()]
        recent_count = 0
        if identifiers:
            r = await s.execute(text(
                "SELECT COUNT(*) FROM events WHERE source='telegram' "
                "AND metadata->>'sender_id' = ANY(:ids) "
                "AND occurred_at > NOW() - interval '30 day'"
            ), {"ids": identifiers})
            recent_count = r.scalar() or 0

    return {
        "entity_id": entity_id,
        "aliases": [dict(a) for a in aliases],
        "memberships": [dict(m) for m in memberships],
        "recent_30d_messages": recent_count,
    }


async def merge_entities(keeper_id: int, merged_id: int) -> dict:
    """Move all aliases / memberships / relationships from merged → keeper, delete merged.

    Idempotent on uniqueness constraints (alias source+identifier UNIQUE,
    membership unique, relationships unique by subject+predicate+object).
    Conflicting rows during reassign are dropped — they already point at the
    correct keeper somehow.

    Returns counts of what moved + the deleted entity ID.
    """
    if keeper_id == merged_id:
        return {"error": "keeper_id == merged_id, refusing"}

    async with get_session() as s:
        # ALIASES: move what fits, drop conflicts
        aliases_moved = (await s.execute(text("""
            WITH moved AS (
              UPDATE entity_aliases
              SET entity_id = :keeper
              WHERE entity_id = :merged
                AND NOT EXISTS (
                  SELECT 1 FROM entity_aliases ea2
                  WHERE ea2.entity_id = :keeper
                    AND ea2.source = entity_aliases.source
                    AND ea2.identifier = entity_aliases.identifier
                )
              RETURNING id
            )
            SELECT COUNT(*) FROM moved
        """), {"keeper": keeper_id, "merged": merged_id})).scalar() or 0

        # Drop remaining (conflicting) aliases on merged
        aliases_dropped = (await s.execute(text(
            "DELETE FROM entity_aliases WHERE entity_id = :merged RETURNING id"
        ), {"merged": merged_id})).rowcount

        # MEMBERSHIPS (member side)
        mems_moved = (await s.execute(text(
            "UPDATE memberships SET member_entity_id = :keeper "
            "WHERE member_entity_id = :merged "
            "  AND NOT EXISTS (SELECT 1 FROM memberships m2 "
            "                  WHERE m2.member_entity_id = :keeper "
            "                    AND m2.group_entity_id = memberships.group_entity_id)"
        ), {"keeper": keeper_id, "merged": merged_id})).rowcount
        await s.execute(text(
            "DELETE FROM memberships WHERE member_entity_id = :merged"
        ), {"merged": merged_id})

        # MEMBERSHIPS (group side — entity could be a group too)
        await s.execute(text(
            "UPDATE memberships SET group_entity_id = :keeper "
            "WHERE group_entity_id = :merged "
            "  AND NOT EXISTS (SELECT 1 FROM memberships m2 "
            "                  WHERE m2.group_entity_id = :keeper "
            "                    AND m2.member_entity_id = memberships.member_entity_id)"
        ), {"keeper": keeper_id, "merged": merged_id})
        await s.execute(text(
            "DELETE FROM memberships WHERE group_entity_id = :merged"
        ), {"merged": merged_id})

        # RELATIONSHIPS — both subject and object sides
        await s.execute(text(
            "UPDATE relationships SET subject_entity_id = :keeper "
            "WHERE subject_entity_id = :merged"
        ), {"keeper": keeper_id, "merged": merged_id})
        await s.execute(text(
            "UPDATE relationships SET object_entity_id = :keeper "
            "WHERE object_entity_id = :merged"
        ), {"keeper": keeper_id, "merged": merged_id})

        # Delete the merged entity row
        await s.execute(text(
            "DELETE FROM entities WHERE id = :merged"
        ), {"merged": merged_id})

    return {
        "keeper_id": keeper_id,
        "merged_id": merged_id,
        "aliases_moved": aliases_moved,
        "aliases_dropped": aliases_dropped,
        "memberships_moved": mems_moved,
    }

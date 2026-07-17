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

import json
import re
import unicodedata

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session


def _as_dict(attrs) -> dict:
    """attributes column → dict. JSONB (Postgres prod) comes back as dict;
    JSON via raw text() on SQLite (tests) comes back as a str — parse it."""
    if isinstance(attrs, str):
        try:
            return json.loads(attrs) or {}
        except ValueError:
            return {}
    return attrs or {}


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


async def find_alias_collisions(min_group: int = 2) -> list[dict]:
    """Groups of DIFFERENT entities sharing one lowercased @username.

    This is the high-precision "real duplicate" signal, distinct from
    `find_duplicates_by_name` (which only catches same first-name namesakes,
    almost always different people). A shared @username means the same
    Telegram object surfaced twice — typically a channel that posts under its
    own handle, creating both a `channel` and a `person` entity for one handle.
    Grouped in Python (not SQL JSON operators) so it stays portable to the
    SQLite test engine.
    """
    async with get_session() as s:
        rows = list((await s.execute(text(
            "SELECT id, name, type, attributes FROM entities"
        ))).mappings().all())

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


def _msg_snippet(content_text: str | None) -> str:
    """Strip the 'Author:/From:/Chat:/Date:/Direction:\n---\n' header the
    ingestor prepends, leaving just the message body for a preview."""
    if not content_text:
        return ""
    body = content_text.split("\n---\n", 1)[-1]
    return " ".join(body.split())[:160]


def _empty_dossier(entity_id: int, name=None, type_=None, username=None,
                   tg_id=None) -> dict:
    return {"entity_id": entity_id, "name": name, "type": type_,
            "username": username, "tg_id": tg_id, "samples": [],
            "top_chats": [], "dom_project": None, "msg_count": 0}


async def get_entity_dossiers(entity_ids: list[int]) -> dict[int, dict]:
    """Batch rich context for MANY entities in 4 queries total (one session).

    Matches a person's messages by the numeric tg_id in
    `metadata->>'sender_id'` (indexed, migration 015). Per-entity fanout
    (a session each) exhausts the connection pool on a page with ~200
    candidates — this fetches samples/chats/projects set-based instead.

    NB: the old `get_entity_context.recent_30d_messages` matched alias
    identifiers (a `user:` prefix, never `.isdigit()`) — it silently returned
    0. tg_id is the correct signal.
    """
    if not entity_ids:
        return {}
    async with get_session() as s:
        ents = (await s.execute(
            text("SELECT id, name, type, attributes FROM entities WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": entity_ids},
        )).mappings().all()

        by_id: dict[int, dict] = {}
        tgid_to_eid: dict[str, int] = {}
        for e in ents:
            attrs = _as_dict(e["attributes"])
            tg_id = attrs.get("tg_id")
            by_id[e["id"]] = _empty_dossier(e["id"], e["name"], e["type"],
                                            attrs.get("username"), tg_id)
            if tg_id is not None:
                tgid_to_eid[str(tg_id)] = e["id"]

        for eid in entity_ids:
            by_id.setdefault(eid, _empty_dossier(eid))

        tgids = list(tgid_to_eid)
        if not tgids:
            return by_id

        sample_rows = (await s.execute(text("""
            SELECT sender_id, content_text FROM (
              SELECT metadata->>'sender_id' AS sender_id, content_text,
                     row_number() OVER (PARTITION BY metadata->>'sender_id'
                                        ORDER BY occurred_at DESC) AS rn
              FROM events
              WHERE source='telegram' AND metadata->>'sender_id' IN :t
            ) x WHERE rn <= 3
        """).bindparams(bindparam("t", expanding=True)), {"t": tgids})).all()
        for sid, content in sample_rows:
            snip = _msg_snippet(content)
            if snip:
                by_id[tgid_to_eid[sid]]["samples"].append(snip)

        chat_rows = (await s.execute(text("""
            SELECT metadata->>'sender_id' AS sender_id,
                   metadata->>'chat_title' AS chat_title, count(*) AS n
            FROM events
            WHERE source='telegram' AND metadata->>'sender_id' IN :t
            GROUP BY sender_id, chat_title
        """).bindparams(bindparam("t", expanding=True)), {"t": tgids})).all()
        chats_by_sid: dict[str, list[tuple[str, int]]] = {}
        for sid, chat, n in chat_rows:
            chats_by_sid.setdefault(sid, []).append((chat or "—", n))
        for sid, chats in chats_by_sid.items():
            chats.sort(key=lambda c: -c[1])
            d = by_id[tgid_to_eid[sid]]
            d["top_chats"] = chats[:3]
            d["msg_count"] = sum(n for _, n in chats)

        proj_rows = (await s.execute(text("""
            SELECT metadata->>'sender_id' AS sender_id, project, count(*) AS n
            FROM events
            WHERE source='telegram' AND metadata->>'sender_id' IN :t
              AND project IS NOT NULL
            GROUP BY sender_id, project
        """).bindparams(bindparam("t", expanding=True)), {"t": tgids})).all()
        best: dict[str, tuple[str, int]] = {}
        for sid, proj, n in proj_rows:
            if sid not in best or n > best[sid][1]:
                best[sid] = (proj, n)
        for sid, (proj, _) in best.items():
            by_id[tgid_to_eid[sid]]["dom_project"] = proj

    return by_id


async def get_entity_dossier(entity_id: int) -> dict:
    """Single-entity convenience wrapper over `get_entity_dossiers`."""
    return (await get_entity_dossiers([entity_id]))[entity_id]


_CHAT_TYPES = {"channel", "supergroup", "group"}


async def merge_username_collision_pairs() -> list[dict]:
    """Авто-слияние однозначных @username-коллизий: ровно 2 сущности с одним
    username, одна — чат (channel/supergroup/group), вторая — person (канал,
    постящий от своего имени, порождал обе до фикса entity_sync). Username в
    Telegram уникален глобально — это детерминированный дубль, не догадка.
    Keeper — чатовая сущность (человекочитаемое имя-заголовок). Неоднозначные
    группы (3+ сущностей, две персоны и т.п.) не трогаем — они остаются в UI.
    Возвращает список слитых пар."""
    merged: list[dict] = []
    for g in await find_alias_collisions(min_group=2):
        cands = g["candidates"]
        chats = [c for c in cands if c["type"] in _CHAT_TYPES]
        people = [c for c in cands if c["type"] == "person"]
        if len(cands) == 2 and len(chats) == 1 and len(people) == 1:
            await merge_entities(chats[0]["id"], people[0]["id"])
            merged.append({"username": g["username"],
                           "keeper": chats[0]["id"], "merged": people[0]["id"]})
    return merged


async def get_entity_context(entity_id: int) -> dict:
    """Pull aliases, membership chats, and recent message count for review UI."""
    async with get_session() as s:
        ent = (await s.execute(text(
            "SELECT name, type, attributes FROM entities WHERE id = :eid"
        ), {"eid": entity_id})).mappings().first()

        aliases = list((await s.execute(text(
            "SELECT source, identifier, display_name FROM entity_aliases "
            "WHERE entity_id = :eid"
        ), {"eid": entity_id})).mappings().all())

        memberships = list((await s.execute(text(
            "SELECT parent_entity_id, role, first_seen_at FROM memberships "
            "WHERE child_entity_id = :eid LIMIT 50"
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

    attrs = _as_dict(ent["attributes"]) if ent else {}
    return {
        "entity_id": entity_id,
        "name": ent["name"] if ent else None,
        "type": ent["type"] if ent else None,
        "username": attrs.get("username"),
        "tg_id": attrs.get("tg_id"),
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
        # ALIASES: move what fits, drop conflicts (rowcount, не CTE —
        # data-modifying CTE не работает в SQLite, на которой идут тесты)
        aliases_moved = (await s.execute(text("""
            UPDATE entity_aliases
            SET entity_id = :keeper
            WHERE entity_id = :merged
              AND NOT EXISTS (
                SELECT 1 FROM entity_aliases ea2
                WHERE ea2.entity_id = :keeper
                  AND ea2.source = entity_aliases.source
                  AND ea2.identifier = entity_aliases.identifier
              )
        """), {"keeper": keeper_id, "merged": merged_id})).rowcount or 0

        # Drop remaining (conflicting) aliases on merged
        aliases_dropped = (await s.execute(text(
            "DELETE FROM entity_aliases WHERE entity_id = :merged RETURNING id"
        ), {"merged": merged_id})).rowcount

        # MEMBERSHIPS (child side) — UNIQUE is (parent, child, source)
        mems_moved = (await s.execute(text(
            "UPDATE memberships SET child_entity_id = :keeper "
            "WHERE child_entity_id = :merged "
            "  AND NOT EXISTS (SELECT 1 FROM memberships m2 "
            "                  WHERE m2.child_entity_id = :keeper "
            "                    AND m2.parent_entity_id = memberships.parent_entity_id "
            "                    AND m2.source = memberships.source)"
        ), {"keeper": keeper_id, "merged": merged_id})).rowcount
        await s.execute(text(
            "DELETE FROM memberships WHERE child_entity_id = :merged"
        ), {"merged": merged_id})

        # MEMBERSHIPS (parent side — entity could be a group too)
        await s.execute(text(
            "UPDATE memberships SET parent_entity_id = :keeper "
            "WHERE parent_entity_id = :merged "
            "  AND NOT EXISTS (SELECT 1 FROM memberships m2 "
            "                  WHERE m2.parent_entity_id = :keeper "
            "                    AND m2.child_entity_id = memberships.child_entity_id "
            "                    AND m2.source = memberships.source)"
        ), {"keeper": keeper_id, "merged": merged_id})
        await s.execute(text(
            "DELETE FROM memberships WHERE parent_entity_id = :merged"
        ), {"merged": merged_id})

        # RELATIONSHIPS — обе стороны, с guard'ами (миграция 017 добавила
        # UNIQUE (subject, predicate, object) — неохраняемый UPDATE ронял бы
        # весь merge IntegrityError'ом). Связь merged↔keeper стала бы
        # самопетлёй keeper→keeper — удаляем, а не переносим.
        await s.execute(text(
            "DELETE FROM relationships "
            "WHERE (subject_entity_id = :merged AND object_entity_id = :keeper) "
            "   OR (subject_entity_id = :keeper AND object_entity_id = :merged)"
        ), {"keeper": keeper_id, "merged": merged_id})
        await s.execute(text(
            "UPDATE relationships SET subject_entity_id = :keeper "
            "WHERE subject_entity_id = :merged "
            "  AND NOT EXISTS (SELECT 1 FROM relationships r2 "
            "                  WHERE r2.subject_entity_id = :keeper "
            "                    AND r2.predicate = relationships.predicate "
            "                    AND r2.object_entity_id = relationships.object_entity_id)"
        ), {"keeper": keeper_id, "merged": merged_id})
        await s.execute(text(
            "UPDATE relationships SET object_entity_id = :keeper "
            "WHERE object_entity_id = :merged "
            "  AND NOT EXISTS (SELECT 1 FROM relationships r2 "
            "                  WHERE r2.subject_entity_id = relationships.subject_entity_id "
            "                    AND r2.predicate = relationships.predicate "
            "                    AND r2.object_entity_id = :keeper)"
        ), {"keeper": keeper_id, "merged": merged_id})
        # Остатки — дубли уже существующих у keeper'а связей
        await s.execute(text(
            "DELETE FROM relationships "
            "WHERE subject_entity_id = :merged OR object_entity_id = :merged"
        ), {"merged": merged_id})

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

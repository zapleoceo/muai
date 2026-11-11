"""Read-модели для ревью дублей: пакетные досье и контекст одной сущности."""
from __future__ import annotations

from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.graph.dedup_store import (
    _as_dict,
    load_dossier_entities,
    load_review_context,
    load_sender_chats,
    load_sender_projects,
    load_sender_samples,
)
from vera_shared.ingest.envelope import message_body


def _msg_snippet(content_text: str | None) -> str:
    """Strip the 'Author:/From:/Chat:/Date:/Direction:\n---\n' header the
    ingestor prepends, leaving just the message body for a preview."""
    return " ".join(message_body(content_text).split())[:160]


def _empty_dossier(entity_id: int, name: str | None = None, type_: str | None = None,
                   username: Any = None, tg_id: Any = None) -> dict:
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
        by_id: dict[int, dict] = {}
        tgid_to_eid: dict[str, int] = {}
        for e in await load_dossier_entities(s, entity_ids):
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

        for sid, content in await load_sender_samples(s, tgids):
            snip = _msg_snippet(content)
            if snip:
                by_id[tgid_to_eid[sid]]["samples"].append(snip)

        chats_by_sid: dict[str, list[tuple[str, int]]] = {}
        for sid, chat, n in await load_sender_chats(s, tgids):
            chats_by_sid.setdefault(sid, []).append((chat or "—", n))
        for sid, chats in chats_by_sid.items():
            chats.sort(key=lambda c: -c[1])
            d = by_id[tgid_to_eid[sid]]
            d["top_chats"] = chats[:3]
            d["msg_count"] = sum(n for _, n in chats)

        best: dict[str, tuple[str, int]] = {}
        for sid, proj, n in await load_sender_projects(s, tgids):
            if sid not in best or n > best[sid][1]:
                best[sid] = (proj, n)
        for sid, (proj, _) in best.items():
            by_id[tgid_to_eid[sid]]["dom_project"] = proj

    return by_id


async def get_entity_dossier(entity_id: int) -> dict:
    """Single-entity convenience wrapper over `get_entity_dossiers`."""
    return (await get_entity_dossiers([entity_id]))[entity_id]


async def get_entity_context(entity_id: int) -> dict:
    """Pull aliases, membership chats, and recent message count for review UI."""
    ent, aliases, memberships, recent_count = await load_review_context(entity_id)
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

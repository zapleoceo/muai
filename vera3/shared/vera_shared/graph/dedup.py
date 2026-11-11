"""Entity deduplication utilities.

Today we have 340+ duplicate entities (15 Alex, 12 Александр, 11 Сергей)
because entity_sync upserts by (source, sender_id) — one person with multiple
TG IDs becomes N rows. This package detects duplicates and merges them
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

The actual decision is owner-driven via dashboard /entities/duplicates UI.

Layout: `dedup_detect` (кандидаты), `dedup_read` (досье и контекст ревью),
`dedup_merge` (слияние), SQL — в `dedup_store` и `dedup_merge_store`. Этот
модуль — фасад: прежние пути импорта (`graph.dedup.merge_entities`, `_as_dict`,
…) работают, и здесь живёт авто-политика слияния @username-коллизий.
"""
from __future__ import annotations

from vera_shared.db.engine import get_session
from vera_shared.graph.dedup_detect import (
    find_duplicates_by_name,
    group_alias_collisions,
    normalize_name,
)
from vera_shared.graph.dedup_merge import merge_entities
from vera_shared.graph.dedup_read import (
    _msg_snippet,
    get_entity_context,
    get_entity_dossier,
    get_entity_dossiers,
)
from vera_shared.graph.dedup_store import _as_dict, load_entity_handles

__all__ = [
    "_as_dict",
    "_msg_snippet",
    "find_alias_collisions",
    "group_alias_collisions",
    "find_duplicates_by_name",
    "get_entity_context",
    "get_entity_dossier",
    "get_entity_dossiers",
    "merge_entities",
    "merge_username_collision_pairs",
    "normalize_name",
]

_CHAT_TYPES = {"channel", "supergroup", "group"}


async def find_alias_collisions(min_group: int = 2) -> list[dict]:
    """Сущности с общим @username (см. `group_alias_collisions`)."""
    # Сессию берём из глобали этого модуля: тесты подменяют `dedup.get_session`.
    return group_alias_collisions(await load_entity_handles(get_session), min_group)


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
            # Через глобаль модуля: тесты подменяют `dedup.merge_entities`.
            await merge_entities(chats[0]["id"], people[0]["id"])
            merged.append({"username": g["username"],
                           "keeper": chats[0]["id"], "merged": people[0]["id"]})
    return merged

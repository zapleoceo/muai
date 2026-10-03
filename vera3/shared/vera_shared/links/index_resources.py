"""Ресурсы сборки связей: люди графа, прозвища, карта голосов, круг владельца.

Меняются редко, поэтому читаются один раз на прогон (`load_resources`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from vera_shared.db.engine import get_session
from vera_shared.links.context_data import established_contacts
from vera_shared.links.matcher import MentionMatcher, PersonNames
from vera_shared.links.names import NameResolver
from vera_shared.links.nicknames import active_rules

log = logging.getLogger(__name__)


@dataclass
class Resources:
    matcher: MentionMatcher
    names: NameResolver
    owner_circle: frozenset[int]
    speaker_map: dict[tuple[str, str], int]
    asr_candidates: dict[int, str]


async def load_resources(owner: int | None) -> Resources:
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT id, name, attributes->>'username' FROM entities WHERE type = 'person'"))).all()
        try:
            mapped = (await s.execute(text(
                "SELECT kind, key, entity_id FROM voice_speaker_map"))).all()
        except DBAPIError as e:
            log.warning("voice_speaker_map не прочитана (миграция 044?): %s", e)
            mapped = []
    persons = [PersonNames(r[0], r[1], (r[2],) if r[2] else ()) for r in rows if r[1]]
    rules = await active_rules()
    strong = {e: await established_contacts(e) for e in {r.entity_id for r in rules}}
    circle = await established_contacts(owner) | ({owner} if owner is not None else frozenset())
    return Resources(MentionMatcher(persons, rules, strong), NameResolver(persons),
                     frozenset(circle), {(r[0], r[1]): r[2] for r in mapped},
                     {p.entity_id: p.name for p in persons if p.entity_id in circle})

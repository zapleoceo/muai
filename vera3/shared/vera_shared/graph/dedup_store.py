"""SQL чтения для дедупа сущностей: имена, @username, досье, контекст ревью.

Слияние пишет отдельный `dedup_merge_store`; здесь только SELECT'ы."""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.events.visibility import NOT_HIDDEN_SQL


def _as_dict(attrs: Any) -> dict:
    """attributes column → dict. JSONB (Postgres prod) comes back as dict;
    JSON via raw text() on SQLite (tests) comes back as a str — parse it."""
    if isinstance(attrs, str):
        try:
            return json.loads(attrs) or {}
        except ValueError:
            return {}
    return attrs or {}


async def load_entity_names() -> list[tuple[int, str]]:
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT id, name FROM entities WHERE name IS NOT NULL"
        ))).all()
    return [tuple(row) for row in rows]


async def load_entity_handles(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = get_session,
) -> list[dict]:
    async with session_factory() as s:
        return [dict(r) for r in (await s.execute(text(
            "SELECT id, name, type, attributes FROM entities"
        ))).mappings().all()]


async def load_dossier_entities(s: AsyncSession, entity_ids: list[int]) -> list[Any]:
    return list((await s.execute(
        text("SELECT id, name, type, attributes FROM entities WHERE id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": entity_ids},
    )).mappings().all())


async def load_sender_samples(s: AsyncSession, tgids: list[str]) -> list[Any]:
    return list((await s.execute(text(f"""
        SELECT sender_id, content_text FROM (
          SELECT metadata->>'sender_id' AS sender_id, content_text,
                 row_number() OVER (PARTITION BY metadata->>'sender_id'
                                    ORDER BY occurred_at DESC) AS rn
          FROM events
          WHERE source='telegram' AND metadata->>'sender_id' IN :t
            AND {NOT_HIDDEN_SQL}
        ) x WHERE rn <= 3
    """).bindparams(bindparam("t", expanding=True)), {"t": tgids})).all())


async def load_sender_chats(s: AsyncSession, tgids: list[str]) -> list[Any]:
    return list((await s.execute(text(f"""
        SELECT metadata->>'sender_id' AS sender_id,
               metadata->>'chat_title' AS chat_title, count(*) AS n
        FROM events
        WHERE source='telegram' AND metadata->>'sender_id' IN :t
          AND {NOT_HIDDEN_SQL}
        GROUP BY sender_id, chat_title
    """).bindparams(bindparam("t", expanding=True)), {"t": tgids})).all())


async def load_sender_projects(s: AsyncSession, tgids: list[str]) -> list[Any]:
    return list((await s.execute(text(f"""
        SELECT metadata->>'sender_id' AS sender_id, project, count(*) AS n
        FROM events
        WHERE source='telegram' AND metadata->>'sender_id' IN :t
          AND project IS NOT NULL AND {NOT_HIDDEN_SQL}
        GROUP BY sender_id, project
    """).bindparams(bindparam("t", expanding=True)), {"t": tgids})).all())


async def load_review_context(entity_id: int) -> tuple[Any, list[Any], list[Any], int]:
    """Сущность, её алиасы, членства и число сообщений за 30 дней — одной сессией."""
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

        # Sender ids of events are numeric tg ids; only digit aliases can match.
        identifiers = [a["identifier"] for a in aliases
                       if a["source"] == "telegram" and a["identifier"].lstrip("-").isdigit()]
        recent_count = 0
        if identifiers:
            r = await s.execute(text(
                "SELECT COUNT(*) FROM events WHERE source='telegram' "
                "AND metadata->>'sender_id' = ANY(:ids) "
                "AND occurred_at > NOW() - interval '30 day' "
                f"AND {NOT_HIDDEN_SQL}"
            ), {"ids": identifiers})
            recent_count = r.scalar() or 0
    return ent, aliases, memberships, recent_count

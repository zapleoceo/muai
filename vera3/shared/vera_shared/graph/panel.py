"""Карточка сущности для боковой панели `/graph`: алиасы, счётчики, связи-пары
и последние события человека.

События ищутся по алиасу каждого источника (как в `dossiers`): telegram, slack
и instagram — по `metadata.sender_id`, gmail — по адресу в `metadata.from`.
Для telegram есть частичный индекс; остальные идут под коротким таймаутом, чтобы
панель не вешала страницу на скане большой таблицы — при таймауте событий
просто нет, остальное показывается.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from vera_shared.db.engine import get_session
from vera_shared.graph.connections import entity_connections
from vera_shared.graph.repo import get_entity, list_relationships
from vera_shared.ingest.envelope import message_body
from vera_shared.links.read import mentioning_events

log = logging.getLogger(__name__)

CONNECTIONS_SHOWN = 12
RAW_RELATIONSHIPS_SHOWN = 8
EVENTS_SHOWN = 5
SNIPPET_CHARS = 160
EVENTS_TIMEOUT_S = 2
_BY_SENDER_ID = ("telegram", "slack", "instagram")
#: что показываем из свободных атрибутов сущности, в порядке чтения
PROFILE_KEYS = ("employer", "role", "location")


def _is_postgres(session: Any) -> bool:
    return getattr(getattr(getattr(session, "bind", None), "dialect", None), "name", "") == "postgresql"


def _iso(value: datetime | str) -> str:
    # Сырой text() на SQLite отдаёт строку, на Postgres — datetime.
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _snippet(content_text: str | None) -> str:
    return " ".join(message_body(content_text).split())[:SNIPPET_CHARS]


async def _aliases(entity_id: int) -> list[tuple[str, str]]:
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT source, identifier FROM entity_aliases WHERE entity_id = :e "
                 "ORDER BY source, identifier"), {"e": entity_id})).all()
    return [(r[0], r[1]) for r in rows]


async def _counts(entity_id: int) -> dict[str, int]:
    async with get_session() as s:
        relationships = (await s.execute(text(
            "SELECT count(*) FROM relationships WHERE is_current AND "
            "(subject_entity_id = :e OR object_entity_id = :e)"), {"e": entity_id})).scalar_one()
        groups = (await s.execute(text(
            "SELECT count(*) FROM memberships WHERE is_current AND child_entity_id = :e"),
            {"e": entity_id})).scalar_one()
        members = (await s.execute(text(
            "SELECT count(*) FROM memberships WHERE is_current AND parent_entity_id = :e"),
            {"e": entity_id})).scalar_one()
    return {"relationships": int(relationships), "groups": int(groups), "members": int(members)}


def _event_query(source: str) -> str:
    if source == "gmail":
        return ("SELECT id, source, occurred_at, content_text FROM events "
                "WHERE source = 'gmail' AND lower(metadata->>'from') LIKE :key "
                "ORDER BY occurred_at DESC LIMIT :n")
    return ("SELECT id, source, occurred_at, content_text FROM events "
            "WHERE source = :src AND metadata->>'sender_id' = :key "
            "ORDER BY occurred_at DESC LIMIT :n")


def _alias_key(source: str, identifier: str) -> str:
    if source == "gmail":
        return f"%{identifier.lower()}%"
    return identifier.removeprefix("user:")


async def _events_for_alias(source: str, identifier: str, limit: int) -> list[dict[str, Any]]:
    params = {"src": source, "key": _alias_key(source, identifier), "n": limit}
    async with get_session() as s:
        try:
            if _is_postgres(s):
                await s.execute(text(f"SET LOCAL statement_timeout = '{EVENTS_TIMEOUT_S}s'"))
            rows = (await s.execute(text(_event_query(source)), params)).mappings().all()
        except DBAPIError as e:
            log.warning("панель: события %s не уложились в %sс: %s", source, EVENTS_TIMEOUT_S, e)
            return []
    return [{"id": r["id"], "source": r["source"],
             "occurred_at": _iso(r["occurred_at"]),
             "snippet": _snippet(r["content_text"])} for r in rows]


async def recent_events(aliases: list[tuple[str, str]], limit: int = EVENTS_SHOWN) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for source, identifier in aliases:
        if source not in (*_BY_SENDER_ID, "gmail"):
            continue
        for event in await _events_for_alias(source, identifier, limit):
            found[event["id"]] = event
    return sorted(found.values(), key=lambda e: e["occurred_at"], reverse=True)[:limit]


def _relationship(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("predicate", "direction", "other_id", "other_name", "other_type")
    return {k: row[k] for k in keys}


async def entity_panel(entity_id: int, *, raw: bool = False) -> dict[str, Any] | None:
    """Всё для панели одним ответом; None — такой сущности нет. `connections` — по
    одной связи на собеседника (`connections.entity_connections`); `raw=True` добавляет
    ещё и записи `relationships` по одной."""
    entity = await get_entity(entity_id)
    if entity is None:
        return None
    attrs = entity.attributes or {}
    aliases = await _aliases(entity_id)
    payload: dict[str, Any] = {
        "id": entity.id, "name": entity.name, "type": entity.type,
        "username": attrs.get("username") or None,
        "tg_id": attrs.get("tg_id") or None,
        "email": attrs.get("email") or None,
        "profile": [str(attrs[k]) for k in PROFILE_KEYS if attrs.get(k)],
        "aliases": [{"source": s, "identifier": i} for s, i in aliases],
        "counts": await _counts(entity_id),
        "connections": await entity_connections(entity_id, limit=CONNECTIONS_SHOWN),
        "events": await recent_events(aliases),
        "mentions": await mentioning_events(entity_id),
    }
    if raw:
        rels = await list_relationships(entity_id, limit=RAW_RELATIONSHIPS_SHOWN)
        payload["relationships"] = [_relationship(r) for r in rels]
    return payload

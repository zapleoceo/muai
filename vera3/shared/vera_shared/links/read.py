"""Чтение связей событий: участники события, совместные события, выборка по фильтру,
«Обсуждения» в карточке человека.

Строки `scope_ok=false` и скрытые события не показываются. Чтение терпит отсутствие
таблицы (код деплоится раньше миграции): пусто.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from vera_shared.db.engine import get_session
from vera_shared.events.visibility import HIDDEN_STATUS, not_hidden_sql
from vera_shared.ingest.envelope import message_body
from vera_shared.links.builders import speakers_of
from vera_shared.links.context import EventView, owner_entity_id
from vera_shared.links.filters import EventFilter, build_where
from vera_shared.links.model import KIND_SOURCES, MENTIONED, PRESENCE_ROLES, ROLES

log = logging.getLogger(__name__)

PREVIEW_CHARS = 300
SNIPPET_CHARS = 240
MENTIONS_SHOWN = 12
MAX_EVENTS = 200
_COLUMNS = ("events.id, events.source, events.account, events.occurred_at, events.content_text, "
            "events.importance, events.project, events.metadata->>'chat_title' AS chat")


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _json(value: Any) -> dict[str, Any] | None:
    """Сырой text() на SQLite отдаёт JSON строкой, на Postgres — словарём."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _preview(r: Any, chars: int | None = PREVIEW_CHARS) -> dict[str, Any]:
    body = " ".join(message_body(r["content_text"]).split())
    return {"id": r["id"], "source": r["source"], "account": r["account"],
            "occurred_at": _iso(r["occurred_at"]), "chat": r["chat"],
            "content_preview": body if chars is None else body[:chars],
            "importance": r["importance"], "project": r["project"]}


async def filtered_events(f: EventFilter, limit: int = 50) -> tuple[list[dict[str, Any]], bool]:
    """События по фильтру, новые первыми; → (список, упёрлись ли в лимит)."""
    owner = await owner_entity_id() if f.with_owner else None
    where, params = build_where(f, owner)
    sql = (f"SELECT {_COLUMNS} FROM events WHERE {not_hidden_sql('events')}"
           f"{' AND ' + where if where else ''} ORDER BY events.occurred_at DESC LIMIT :lim")
    async with get_session() as s:
        try:
            rows = (await s.execute(text(sql), {**params, "lim": limit + 1})).mappings().all()
        except DBAPIError as e:
            log.warning("event_entities не прочитана (миграция 042?): %s", e)
            return [], False
    return [_preview(r) for r in rows[:limit]], len(rows) > limit


async def entity_events(entity_id: int, start: datetime, end: datetime, limit: int,
                        roles: tuple[str, ...] = ROLES) -> list[dict[str, Any]] | None:
    """События человека за [start, end) в любых из `roles` (написал, получил, был, упомянут);
    у каждого — роли и откуда это известно. None — индекс связей недоступен."""
    wanted = ", ".join(f"'{r}'" for r in roles if r in ROLES)
    sql = (f"SELECT {_COLUMNS}, l.role, l.source_of_link FROM event_entities l "
           "JOIN events ON events.id = l.event_id WHERE l.entity_id = :e AND l.scope_ok "
           f"AND l.role IN ({wanted}) AND {not_hidden_sql('events')} "
           "AND events.occurred_at >= :a AND events.occurred_at < :b "
           "ORDER BY events.occurred_at DESC LIMIT :n")
    async with get_session() as s:
        try:
            rows = (await s.execute(text(sql), {"e": entity_id, "a": start, "b": end,
                                                "n": limit * 3})).mappings().all()
        except DBAPIError as e:
            log.warning("event_entities не прочитана (миграция 042?): %s", e)
            return None
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        item = out.setdefault(r["id"], {**_preview(r), "roles": [], "via": []})
        if r["role"] not in item["roles"]:
            item["roles"].append(r["role"])
        if r["source_of_link"] not in item["via"]:
            item["via"].append(r["source_of_link"])
    return list(out.values())[:limit]


async def co_occurrence(entity_a: int, entity_b: int, start: datetime | None = None,
                        end: datetime | None = None, limit: int = 50) -> dict[str, Any]:
    """События, где были ОБА (автор / получатель / участник): сводка по видам и список."""
    f = EventFilter(participant_ids=(entity_a, entity_b), start=start, end=end)
    events, truncated = await filtered_events(f, limit)
    by_kind = {kind: sum(1 for e in events if e["source"] in sources)
               for kind, sources in KIND_SOURCES.items()}
    return {"count": len(events), "truncated": truncated, "by_kind": by_kind, "events": events}


async def event_participants(event_id: int) -> dict[str, Any] | None:
    """Кто связан с событием и как; None — события нет или оно скрыто. Неопознанные ярлыки голосов
    созвона перечислены отдельно: их можно назвать через `voice_speaker_set`."""
    async with get_session() as s:
        ev = (await s.execute(text(
            "SELECT id, source, occurred_at, metadata, content_extra, triage_status FROM events "
            "WHERE id = :i"),
            {"i": event_id})).mappings().first()
        if ev is None or ev["triage_status"] == HIDDEN_STATUS:
            return None                       # скрытое событие и его участники — как несуществующие
        try:
            rows = (await s.execute(text(
                "SELECT l.entity_id, e.name, l.role, l.source_of_link, l.confidence, l.token, "
                "l.span, l.scope_ok FROM event_entities l JOIN entities e ON e.id = l.entity_id "
                "WHERE l.event_id = :i ORDER BY l.role, l.confidence DESC"),
                {"i": event_id})).mappings().all()
        except DBAPIError as e:
            log.warning("event_entities не прочитана (миграция 042?): %s", e)
            rows = []
    people: dict[int, dict[str, Any]] = {}
    for r in rows:
        if not r["scope_ok"]:
            continue
        p = people.setdefault(r["entity_id"], {"entity_id": r["entity_id"], "name": r["name"],
                                               "roles": [], "links": []})
        if r["role"] not in p["roles"]:
            p["roles"].append(r["role"])
        p["links"].append({"role": r["role"], "via": r["source_of_link"],
                           "confidence": round(float(r["confidence"]), 2),
                           "token": r["token"] or None, "span": r["span"]})
    linked = {r["token"] for r in rows if r["token"]}
    meta, extra = _json(ev["metadata"]) or {}, _json(ev["content_extra"])
    view = EventView(ev["id"], ev["source"], "", meta, extra)
    unresolved = [{"label": label, "utterances": n} for label, n in speakers_of(view).items()
                  if label not in linked]
    return {"event_id": ev["id"], "source": ev["source"], "occurred_at": _iso(ev["occurred_at"]),
            "participants": [p for p in people.values() if set(p["roles"]) & set(PRESENCE_ROLES)],
            "mentioned": [p for p in people.values() if p["roles"] == [MENTIONED]],
            "unresolved_speakers": unresolved}


_MENTIONS_SQL = (
    "SELECT events.id, events.source, events.occurred_at, events.content_text, "
    "events.metadata->>'chat_title' AS chat, l.token, l.source_of_link, l.confidence "
    "FROM event_entities l JOIN events ON events.id = l.event_id "
    f"WHERE l.entity_id = :e AND l.role = 'mentioned' AND l.scope_ok AND {not_hidden_sql('events')} "
    "AND l.confidence >= :min ORDER BY events.occurred_at DESC LIMIT :n")


async def mentioning_events(entity_id: int, limit: int = MENTIONS_SHOWN, *,
                            min_confidence: float = 0.0,
                            snippet_chars: int | None = SNIPPET_CHARS) -> list[dict[str, Any]]:
    """Свежие события, где человека УПОМЯНУЛИ (не его собственные сообщения)."""
    async with get_session() as s:
        try:
            rows = (await s.execute(text(_MENTIONS_SQL), {"e": entity_id, "n": limit,
                                                          "min": min_confidence})).mappings().all()
        except DBAPIError as e:
            log.warning("event_entities не прочитана (миграция 042?): %s", e)
            return []
    return [{**_preview({**r, "account": None, "importance": None, "project": None},
                        snippet_chars),
             "token": r["token"], "via": r["source_of_link"],
             "confidence": round(float(r["confidence"]), 2)} for r in rows]


async def mention_counts(entity_id: int) -> dict[str, int]:
    """События с упоминанием в области и прозвищные упоминания, отброшенные как «вне области»."""
    async with get_session() as s:
        try:
            rows = (await s.execute(text(
                "SELECT scope_ok, count(DISTINCT event_id) FROM event_entities "
                "WHERE entity_id = :e AND role = 'mentioned' GROUP BY scope_ok"),
                {"e": entity_id})).all()
        except DBAPIError as e:
            log.warning("event_entities не прочитана (миграция 042?): %s", e)
            return {"in_scope": 0, "out_of_scope": 0}
    by = {bool(ok): int(n) for ok, n in rows}
    return {"in_scope": by.get(True, 0), "out_of_scope": by.get(False, 0)}

"""MCP-инструменты чтения: поиск, события, сущности, граф, SQL.

Каждая выдача ограничена и несёт признак `truncated`, чтобы агент знал, что
видит не всё. Скрытые события в списках не показываются; `get_event`
отдаёт их с `hidden: true`.
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field
from vera_shared.events import queries
from vera_shared.events.visibility import HIDDEN_STATUS
from vera_shared.graph import event_links
from vera_shared.graph.context import entity_context_payload
from vera_shared.graph.repo import find_entity_by_name, graph_snapshot
from vera_shared.graph.search import search_entities
from vera_shared.journal import audit
from vera_shared.links.filters import to_dict
from vera_shared.links.model import ROLES
from vera_shared.links.read import entity_events, filtered_events
from vera_shared.search_client import SearchUnavailable, search_brain
from vera_shared.timeutil import parse_iso_naive, utc_naive_now

from vera_mcp.link_tools import LINK_TOOLS, IdList, Kind, Role, link_filter
from vera_mcp.sql_guard import MAX_ROWS, run_readonly

MAX_EVENTS = 200
MAX_EDGES = 300
MAX_MEMBERS = 50
DEFAULT_TEXT_CHARS = 8_000


def _search_conf() -> tuple[str, str]:
    return (os.environ.get("SEARCH_URL", "http://brain-search:8000"),
            os.environ.get("INTERNAL_SECRET", ""))


async def search(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    participant_ids: IdList = None, mentioned_ids: IdList = None, author_ids: IdList = None,
    with_owner: bool = False, source: str | None = None, kind: Kind | None = None,
    start: str | None = None, end: str | None = None,
) -> dict[str, Any]:
    """Гибридный поиск (смысл + полнотекст) по всему мозгу. Фильтры (все через AND): participant_ids — где были ВСЕ эти люди (автор/получатель/участник созвона), mentioned_ids — где их упомянули, author_ids — где писали они, with_owner — где был владелец, kind — call|message|email, source, start/end (ISO). Hybrid search; the people filters narrow candidates BEFORE ranking."""
    flt = link_filter(participant_ids, mentioned_ids, author_ids, with_owner, source, kind, start, end)
    url, secret = _search_conf()
    try:
        data = await search_brain(url, secret, query, limit,
                                  **({"filters": to_dict(flt)} if not flt.empty else {}))
    except SearchUnavailable as e:
        raise RuntimeError(e.detail) from e
    results = list(data.get("results") or [])
    return {"results": results[:limit], "count": min(len(results), limit),
            "truncated": len(results) > limit}


async def recent_events(
    hours: Annotated[int, Field(ge=1, le=720)] = 24,
    source: str | None = None, account: str | None = None, project: str | None = None,
    limit: Annotated[int, Field(ge=1, le=MAX_EVENTS)] = 50,
    participant_ids: IdList = None, mentioned_ids: IdList = None, author_ids: IdList = None,
    with_owner: bool = False, kind: Kind | None = None,
) -> dict[str, Any]:
    """Свежие события с фильтрами по источнику/аккаунту/проекту и по людям (participant_ids, mentioned_ids, author_ids, with_owner, kind=call|message|email — через AND). Recent events, newest first."""
    flt = link_filter(participant_ids, mentioned_ids, author_ids, with_owner, source, kind,
                      start=(utc_naive_now() - timedelta(hours=hours)).isoformat())
    if flt.uses_links or kind:
        flt = replace(flt, account=account, project=project)
        events, truncated = await filtered_events(flt, limit)
        return {"count": len(events), "truncated": truncated, "events": events}
    events, truncated = await queries.recent_events(
        hours=hours, limit=limit, source=source, account=account, project=project)
    return {"count": len(events), "truncated": truncated, "events": events}


async def get_event(
    event_id: int,
    max_chars: Annotated[int, Field(ge=100, le=50_000)] = DEFAULT_TEXT_CHARS,
) -> dict[str, Any]:
    """Событие целиком: текст, метаданные, триаж, связанные сущности. One event in full, with triage info and linked entities."""
    row = await queries.get_event_row(event_id)
    if row is None:
        raise LookupError(f"event {event_id} not found")
    text = row.content_text or ""
    transcript = row.transcript_text or ""
    return {
        "id": row.id, "source": row.source, "source_event_id": row.source_event_id,
        "account": row.account, "category": row.category,
        "occurred_at": row.occurred_at.isoformat(),
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "content_text": text[:max_chars], "content_truncated": len(text) > max_chars,
        "transcript_text": transcript[:max_chars],
        "transcript_truncated": len(transcript) > max_chars,
        "metadata": row.metadata_, "content_extra": row.content_extra,
        "hidden": row.triage_status == HIDDEN_STATUS,
        "triage": {"status": row.triage_status, "error": row.triage_error,
                   "importance": row.importance, "nature": row.nature,
                   "project": row.project, "metadata": row.triage_metadata},
        "entities": await event_links.linked_entities(event_id),
    }


async def list_sources() -> dict[str, Any]:
    """Сводка по источникам: число событий, последнее событие и последний приём. Per-source counts and freshness."""
    sources = await queries.source_stats()
    return {"sources": sources, "total_events": sum(s["events"] for s in sources)}


async def entity_find(
    query: Annotated[str, Field(min_length=2, max_length=200)],
    type: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Найти сущности по имени, алиасу, username или email. Fuzzy entity lookup."""
    found = await search_entities(query, limit=limit + 1, type=type)
    return {"entities": found[:limit], "truncated": len(found) > limit}


async def entity_context(
    entity_id: int | None = None,
    name: Annotated[str | None, Field(min_length=2)] = None,
    raw_relationships: bool = False, include_mentions: bool = False,
) -> dict[str, Any]:
    """Что известно о сущности: алиасы, членства, связи-пары (роли с весом и взаимодействиями), активность (по id или имени). raw_relationships=true добавляет записи relationships по одной (с id для relationship_retire); include_mentions=true — события, где человека упомянули (не его сообщения), с источником связи, и счёт «в области / вне области» для прозвищ. Everything known about one entity; connections are one per counterpart."""
    if entity_id is None:
        if not name:
            raise ValueError("pass entity_id or name")
        entity_id = await find_entity_by_name(name)
        if entity_id is None:
            raise LookupError(f"no entity matching '{name}'")
    payload = await entity_context_payload(entity_id, raw_relationships=raw_relationships,
                                           include_mentions=include_mentions)
    if payload is None:
        raise LookupError(f"entity {entity_id} not found")
    members = payload["members"]
    payload["members"] = members[:MAX_MEMBERS]
    payload["members_truncated"] = len(members) > MAX_MEMBERS
    return payload


async def graph_neighbours(
    entity_id: int, predicate: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
    raw_edges: bool = False,
) -> dict[str, Any]:
    """Соседи сущности в графе (1 шаг): одно ребро на пару с главной ролью, весом и «also»; членства. raw_edges=true — по одному ребру на запись relationships. One-hop neighbours; one edge per pair."""
    snap = await graph_snapshot(focus_id=entity_id, limit=limit, predicate=predicate,
                                raw_edges=raw_edges)
    edges = snap["edges"]
    return {"nodes": snap["nodes"], "edges": edges[:MAX_EDGES],
            "truncated": len(edges) > MAX_EDGES or len(snap["nodes"]) >= limit}


async def timeline(
    entity_id: int, start: str | None = None, end: str | None = None,
    limit: Annotated[int, Field(ge=1, le=MAX_EVENTS)] = 50,
    roles: Annotated[list[Role] | None, Field(max_length=4)] = None,
) -> dict[str, Any]:
    """События сущности за период (ISO-даты; по умолчанию последние 30 дней): написанные ею, адресованные ей, где она участвовала (созвоны) и где её упомянули (имя, фамилия, @ник, прозвище в области); roles сужает до author/recipient/participant/mentioned. У события — roles и via. Events by/about an entity in a date range."""
    now = utc_naive_now()
    t_end = parse_iso_naive(end) if end else now
    t_start = parse_iso_naive(start) if start else t_end - timedelta(days=30)
    events = await entity_events(entity_id, t_start, t_end, limit + 1,
                                 tuple(roles) if roles else ROLES) or []
    if not roles:   # события, до которых индекс ещё не дошёл, берёт прежний поиск по алиасу и имени
        known = {e["id"] for e in events}
        legacy = await event_links.timeline_events(entity_id, t_start, t_end, limit + 1)
        events = sorted(events + [e for e in legacy if e["id"] not in known],
                        key=lambda e: e["occurred_at"], reverse=True)
    return {"events": events[:limit], "truncated": len(events) > limit,
            "start": t_start.isoformat(), "end": t_end.isoformat()}


async def sql_query(
    sql: Annotated[str, Field(min_length=1, max_length=20_000)],
    max_rows: Annotated[int, Field(ge=1, le=MAX_ROWS)] = 100,
) -> dict[str, Any]:
    """Произвольный READ-ONLY запрос к базе (один SELECT/WITH; таймаут 10 с; не больше 500 строк). Escape hatch for ad-hoc analysis; writes are rejected."""
    return await run_readonly(sql, max_rows)


async def audit_log(
    limit: Annotated[int, Field(ge=1, le=100)] = 20, client: str | None = None,
) -> dict[str, Any]:
    """Журнал правок агентов (для undo): кто, что, над чем. Recent write operations with audit ids."""
    return {"entries": await audit.list_entries(limit, client)}


READ_TOOLS = (search, recent_events, get_event, list_sources, entity_find,
              entity_context, graph_neighbours, timeline, sql_query, audit_log, *LINK_TOOLS)

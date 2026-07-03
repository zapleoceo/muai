"""Read endpoints for the vera-mcp bridge — vera_recall / vera_recent / vera_context.

These back the read-side tools in ~/.claude/mcp-servers/vera-mcp/server.py
(docs/mcp-claude.md). Write-side (vera_remember) lives in gateway/claude.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import EntityRow
from vera_shared.graph.dedup import get_entity_context
from vera_shared.graph.repo import find_entity_by_name, list_members

from gateway.config import get_settings

log = logging.getLogger(__name__)
router = APIRouter()

RECENT_EVENTS_LIMIT = 200
RELATIONSHIPS_LIMIT = 50


def _check_internal_secret(provided: str | None) -> None:
    expected = get_settings().internal_secret
    if expected and provided != expected:
        raise HTTPException(401, "invalid internal secret")


# ─── vera_recall → POST /v1/search (proxy to brain-search) ──────────────────


class SearchProxyRequest(BaseModel):
    q: str = Field(min_length=1)
    limit: int = Field(default=15, ge=1, le=50)
    use_agent: bool = False


@router.post("/v1/search")
async def search_proxy(
    body: SearchProxyRequest,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Thin proxy to brain-search's /search — no logic here, just auth + forward."""
    _check_internal_secret(x_internal_secret)

    url = f"{get_settings().search_url}/search"
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, json=body.model_dump())
    except httpx.HTTPError as e:
        raise HTTPException(502, f"brain-search unreachable: {e}") from e

    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"brain-search error: {r.text[:300]}")
    return r.json()


# ─── vera_recent → GET /v1/events/recent ─────────────────────────────────────


@router.get("/v1/events/recent")
async def recent_events(
    hours: int = Query(default=24, ge=1, le=168),
    source: str | None = None,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_internal_secret(x_internal_secret)

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    async with get_session() as s:
        q = select(EventRow).where(EventRow.occurred_at >= since)
        if source:
            q = q.where(EventRow.source == source)
        q = q.order_by(EventRow.occurred_at.desc()).limit(RECENT_EVENTS_LIMIT)
        rows = (await s.execute(q)).scalars().all()

    return {
        "count": len(rows),
        "truncated": len(rows) == RECENT_EVENTS_LIMIT,
        "events": [
            {
                "id": r.id,
                "source": r.source,
                "account": r.account,
                "occurred_at": r.occurred_at.isoformat(),
                "content_preview": (r.content_text or "")[:300],
                "importance": r.importance,
                "project": r.project,
            }
            for r in rows
        ],
    }


# ─── vera_context → GET /v1/entity/context ───────────────────────────────────


_RELATIONSHIPS_SQL = text("""
    SELECT r.predicate, r.fact, r.confidence,
           CASE WHEN r.subject_entity_id = :eid THEN 'out' ELSE 'in' END AS direction,
           CASE WHEN r.subject_entity_id = :eid THEN eo.name ELSE es.name END AS other_name,
           CASE WHEN r.subject_entity_id = :eid THEN eo.type ELSE es.type END AS other_type
    FROM relationships r
    JOIN entities es ON es.id = r.subject_entity_id
    JOIN entities eo ON eo.id = r.object_entity_id
    WHERE (r.subject_entity_id = :eid OR r.object_entity_id = :eid)
      AND r.is_current
    ORDER BY r.confidence DESC
    LIMIT :limit
""")


@router.get("/v1/entity/context")
async def entity_context(
    name: str = Query(min_length=2),
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_internal_secret(x_internal_secret)

    entity_id = await find_entity_by_name(name)
    if entity_id is None:
        raise HTTPException(404, f"no entity matching '{name}'")

    async with get_session() as s:
        entity = await s.get(EntityRow, entity_id)
        rel_rows = (await s.execute(
            _RELATIONSHIPS_SQL, {"eid": entity_id, "limit": RELATIONSHIPS_LIMIT},
        )).mappings().all()

    ctx = await get_entity_context(entity_id)
    members = await list_members(entity_id)

    return {
        "entity_id": entity_id,
        "name": entity.name,
        "type": entity.type,
        "canonical_id": entity.canonical_id,
        "attributes": entity.attributes,
        "last_seen_at": entity.last_seen_at.isoformat(),
        "aliases": ctx["aliases"],
        "memberships": ctx["memberships"],
        "members": members,
        "recent_30d_messages": ctx["recent_30d_messages"],
        "relationships": [dict(r) for r in rel_rows],
    }

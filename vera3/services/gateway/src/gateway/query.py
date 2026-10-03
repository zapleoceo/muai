"""Read endpoints for the vera-mcp bridge — vera_recall / vera_recent / vera_context.

Тонкий HTTP-слой: логика живёт в vera_shared (search_client, events.queries,
graph.context) и общая с удалённым MCP-сервером (`services/mcp`).
Write-side (vera_remember) — gateway/claude.py.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from vera_shared.events.queries import recent_events as query_recent_events
from vera_shared.graph.context import RELATIONSHIPS_LIMIT, entity_context_payload
from vera_shared.graph.repo import find_entity_by_name
from vera_shared.search_client import SearchUnavailable, search_brain

from gateway.auth import check_internal_secret
from gateway.config import get_settings

router = APIRouter()

RECENT_EVENTS_LIMIT = 200

__all__ = ["RECENT_EVENTS_LIMIT", "RELATIONSHIPS_LIMIT", "router"]


class SearchProxyRequest(BaseModel):
    q: str = Field(min_length=1)
    limit: int = Field(default=15, ge=1, le=50)
    use_agent: bool = False


@router.post("/v1/search")
async def search_proxy(
    body: SearchProxyRequest,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_internal_secret(x_internal_secret)
    settings = get_settings()
    try:
        return await search_brain(settings.search_url, settings.internal_secret,
                                  body.q, body.limit, body.use_agent)
    except SearchUnavailable as e:
        raise HTTPException(e.status, e.detail) from e


@router.get("/v1/events/recent")
async def recent_events(
    hours: int = Query(default=24, ge=1, le=168),
    source: str | None = None,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_internal_secret(x_internal_secret)
    events, truncated = await query_recent_events(
        hours=hours, limit=RECENT_EVENTS_LIMIT, source=source)
    return {"count": len(events), "truncated": truncated, "events": events}


@router.get("/v1/entity/context")
async def entity_context(
    name: str = Query(min_length=2),
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    check_internal_secret(x_internal_secret)
    entity_id = await find_entity_by_name(name)
    if entity_id is None:
        raise HTTPException(404, f"no entity matching '{name}'")
    payload = await entity_context_payload(entity_id)
    if payload is None:
        raise HTTPException(404, f"entity {entity_id} vanished")
    return payload

"""Knowledge-graph visualizer — `/graph` page + `/api/graph` JSON.

Renders Vera's L1 substrate (entities + relationships) as an interactive
force-directed graph via Cytoscape.js (loaded from CDN, same pattern as
htmx/telegram-widget elsewhere in the dashboard). The whole graph is 8k+
entities / 7k edges — far too much to draw at once — so the page shows the
*connected core* (degree filter) by default and lets you tap a node to
drill into its ego network. DB access stays in vera_shared.graph.repo.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from vera_shared.graph.clusters import get_clusters, recompute_clusters
from vera_shared.graph.connections import CONNECTIONS_LIMIT
from vera_shared.graph.panel import entity_aliases, entity_panel
from vera_shared.graph.panel_events import recent_events_status
from vera_shared.graph.rel_canon import INVERSE
from vera_shared.graph.rel_extract import PREDICATES
from vera_shared.graph.repo import (
    find_entity_by_alias,
    find_entity_by_name,
    get_entity,
    graph_snapshot,
)
from vera_shared.graph.suggestions import count_pending_suggestions

from dashboard.auth import OWNER_ID
from dashboard.graph_labels import predicate_label, role_label
from dashboard.graph_page import graph_body
from dashboard.render import _render, owner_or_auth_error, owner_or_blank_401

log = logging.getLogger(__name__)
router = APIRouter()

_recluster: dict = {"running": False}
_bg_tasks: set[asyncio.Task] = set()   # ссылки — иначе GC может убить задачу

# Фильтр строится из предикатов, которые реально хранятся: `reports_to` и
# `child_of` записываются как `boss_of` и `parent_of` (`rel_canon`), пункты с
# ними были бы пустыми. Подписи для показа остаются в `graph_labels`. API
# принимает любую строку; member_of — синтетический предикат membership-рёбер.
_PREDICATES = ["member_of", *(p for p in PREDICATES if p not in INVERSE)]


@router.get("/api/graph", response_class=JSONResponse)
async def graph_data(
    request: Request,
    min_degree: int = Query(2, ge=1, le=50),
    limit: int = Query(300, ge=1, le=800),
    predicate: str | None = None,
    focus: int | None = None,
    q: str | None = None,
):
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    focus_id = focus
    if focus_id is None and q and q.strip():
        focus_id = await find_entity_by_name(q.strip())
    snap = await graph_snapshot(
        min_degree=min_degree, limit=limit,
        predicate=predicate or None, focus_id=focus_id,
    )
    snap["focus_id"] = focus_id

    # Тематические сообщества Веры (пересчитываются кнопкой, кэш в app_control)
    clusters = await get_clusters()
    if clusters:
        assign = clusters.get("assign", {})
        for n in snap["nodes"]:
            n["cluster"] = assign.get(str(n["id"]))
        snap["cluster_labels"] = clusters.get("labels", {})
        snap["clusters_at"] = clusters.get("computed_at")
    snap["recluster_running"] = _recluster["running"]
    return JSONResponse(snap)


async def _recluster_bg() -> None:
    try:
        await recompute_clusters()
    except Exception as e:
        log.exception("graph recluster failed: %s", e)
    finally:
        _recluster["running"] = False


@router.post("/graph/recluster")
async def graph_recluster(request: Request):
    """Кнопка «Раскрасить по темам»: label-propagation по связям + LLM-ярлыки.
    Работает в фоне — страница просто перезагружается и подтянет результат."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    if not _recluster["running"]:
        _recluster["running"] = True
        t = asyncio.create_task(_recluster_bg())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    return RedirectResponse("/graph", status_code=303)


async def _owner_fields() -> dict[str, object]:
    """Владелец как сущность графа: от его лица указывается связь «со мной»."""
    owner_id = await find_entity_by_alias("telegram", f"user:{OWNER_ID}")
    owner = await get_entity(owner_id) if owner_id else None
    return {"owner_id": owner.id if owner else None, "owner_name": owner.name if owner else None}


@router.get("/api/graph/entity/{entity_id}/events", response_class=JSONResponse)
async def graph_entity_events(request: Request, entity_id: int):
    """Последние события человека отдельно от карточки: запрос по алиасам медленнее остального."""
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    aliases = await entity_aliases(entity_id)
    events, partial = await recent_events_status(aliases)
    return JSONResponse({"events": events, "partial": partial})


@router.get("/api/graph/entity/{entity_id}", response_class=JSONResponse)
async def graph_entity(request: Request, entity_id: int, raw: bool = False,
                       events: bool = True, all: bool = False):
    """Карточка сущности для боковой панели: алиасы, счётчики, связи-пары, события.
    `raw=true` добавляет записи relationships по одной."""
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    options: dict[str, object] = {}
    if not events:
        options["with_events"] = False
    if all:
        options["connections_limit"] = CONNECTIONS_LIMIT
    panel = await entity_panel(entity_id, raw=raw, **options)
    if panel is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    panel.update(await _owner_fields())
    for conn in panel["connections"]:
        for role in (conn["main"], *conn["also"]):
            role["label"] = role_label(role["predicate"], role["direction"])
    for rel in panel.get("relationships", []):
        rel["label"] = predicate_label(rel["predicate"])
    return JSONResponse(panel)


async def _pending_pairs() -> int | None:
    try:
        return await count_pending_suggestions()
    except SQLAlchemyError as e:
        log.warning("graph: число дублей не прочитано: %s", e)
        return None


@router.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    return HTMLResponse(_render("graph", graph_body(_PREDICATES, await _pending_pairs()), wide=True))

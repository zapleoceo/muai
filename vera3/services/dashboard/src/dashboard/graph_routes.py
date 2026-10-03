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
from vera_shared.graph.panel import entity_panel
from vera_shared.graph.rel_canon import INVERSE
from vera_shared.graph.rel_extract import PREDICATES
from vera_shared.graph.repo import find_entity_by_name, graph_snapshot
from vera_shared.graph.suggestions import count_pending_suggestions

from dashboard.graph_labels import predicate_label
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


@router.get("/api/graph/entity/{entity_id}", response_class=JSONResponse)
async def graph_entity(request: Request, entity_id: int):
    """Карточка сущности для боковой панели: алиасы, счётчики, связи, события."""
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    panel = await entity_panel(entity_id)
    if panel is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    for rel in panel["relationships"]:
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
    return HTMLResponse(_render("graph", graph_body(_PREDICATES, await _pending_pairs())))

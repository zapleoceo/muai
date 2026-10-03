"""Маршруты дублей (`/entities/duplicates`, `/entities/merge` и массовые кнопки)
и аватарки (`/entities/{id}/avatar`). Данные — `duplicates_repo`, разметка —
`entities_view`."""
from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from vera_shared.graph.avatars import get_avatar
from vera_shared.graph.dedup import get_entity_context, merge_username_collision_pairs
from vera_shared.graph.identity import run_identity_analysis, set_suggestion_status
from vera_shared.graph.merge import merge_entities

from dashboard.duplicates_repo import load_duplicates
from dashboard.entities_view import duplicates_body
from dashboard.render import _render, initials_avatar_svg, owner_or_auth_error
from dashboard.ui.theme import DUPES_CSS

log = logging.getLogger(__name__)


async def _merge_with_report(keep_id: int, drop_id: int, reason: str) -> None:
    """Один путь слияния с отчётом; отчёт пишем в лог — на странице его
    негде хранить, а откат по нему делает `merge_graph_duplicates.py --undo`."""
    report = await merge_entities(keep_id, [drop_id], reason)
    log.info("merge report: %s", json.dumps(report.to_dict(), ensure_ascii=False))

# Один анализ за раз; состояние живёт в процессе дашборда (single-owner UI).
_analysis: dict = {"running": False, "last": None}
_bg_tasks: set[asyncio.Task] = set()   # ссылки — иначе GC может убить задачу

TELEGRAM_TOOLS_URL = os.environ.get("TELEGRAM_TOOLS_URL",
                                    "http://ingestor-telegram:8000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")

router = APIRouter()


@router.get("/entities/{entity_id}/avatar")
async def entity_avatar(entity_id: int, request: Request):
    """Serve the entity's profile photo, or a deterministic initials SVG when
    none is stored yet. Owner-gated like every dashboard route."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    got = await get_avatar(entity_id)
    if got is not None:
        image, mime = got
        return Response(content=image, media_type=mime,
                        headers={"Cache-Control": "public, max-age=86400"})
    ctx = await get_entity_context(entity_id)
    svg = initials_avatar_svg(ctx.get("name"), seed=entity_id)
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


async def _run_analysis_bg() -> None:
    try:
        _analysis["last"] = await run_identity_analysis()
    except Exception as e:
        log.exception("identity analysis failed: %s", e)
    finally:
        _analysis["running"] = False


@router.post("/entities/analyze")
async def entities_analyze(request: Request):
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    if not _analysis["running"]:
        _analysis["running"] = True
        t = asyncio.create_task(_run_analysis_bg())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    return RedirectResponse("/entities/duplicates", status_code=303)


@router.post("/entities/merge-email-dupes")
async def entities_merge_email_dupes(request: Request):
    """Слить дубли по рабочему email — он глобально уникален, поэтому две
    person-сущности на один адрес это детерминированный дубль, а не догадка.
    Группы из трёх и более не трогаются: там разбирать глазами."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    from vera_shared.graph.collisions import merge_email_collision_pairs
    done = await merge_email_collision_pairs()
    log.info("email-collision bulk merge: %d pairs", len(done))
    return RedirectResponse("/entities/duplicates", status_code=303)


@router.post("/entities/merge-collisions")
async def entities_merge_collisions(request: Request):
    """Слить все однозначные @username-коллизии (канал+персона) одним махом."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    merged = await merge_username_collision_pairs()
    log.info("username-collision bulk merge: %d pairs", len(merged))
    return RedirectResponse("/entities/duplicates", status_code=303)


@router.post("/entities/roster-sync")
async def entities_roster_sync(request: Request):
    """Молчуны проектных групп → граф: проксируем команду юзерботу
    (tools-сервер, троттлинг и анти-бан — на его стороне)."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{TELEGRAM_TOOLS_URL}/tools/sync_project_rosters",
                headers={"X-Internal-Secret": INTERNAL_SECRET},
            )
        log.info("roster sync trigger: %s %s", r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        log.warning("roster sync trigger failed: %s", e)
    return RedirectResponse("/entities/duplicates", status_code=303)


@router.post("/entities/suggestion")
async def entities_suggestion(request: Request,
                              suggestion_id: int = Form(...),  # noqa: B008
                              action: str = Form(...)):  # noqa: B008
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    status = "rejected" if action == "reject" else "accepted"
    row = await set_suggestion_status(suggestion_id, status)
    if row and action in ("accept_a", "accept_b"):
        keeper = row["entity_a"] if action == "accept_a" else row["entity_b"]
        merged = row["entity_b"] if action == "accept_a" else row["entity_a"]
        await _merge_with_report(keeper, merged, f"dashboard: предложение {suggestion_id}")
    return RedirectResponse("/entities/duplicates", status_code=303)


@router.get("/entities/duplicates", response_class=HTMLResponse)
async def entity_duplicates_page(request: Request, merged: int | None = None):
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    data = await load_duplicates()
    return HTMLResponse(_render("entities", f"<style>{DUPES_CSS}</style>"
                                + duplicates_body(data, _analysis, merged)))


@router.post("/entities/merge")
async def entity_merge(request: Request,
                       keeper_id: int = Form(...),  # noqa: B008
                       merged_id: int = Form(...)):  # noqa: B008
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    await _merge_with_report(keeper_id, merged_id, "dashboard: ручное слияние")
    return RedirectResponse(
        f"/entities/duplicates?merged={merged_id}",
        status_code=303,
    )

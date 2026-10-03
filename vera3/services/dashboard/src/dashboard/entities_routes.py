"""Маршруты дублей (`/entities/duplicates`, `/entities/merge` и массовые кнопки)
и аватарки (`/entities/{id}/avatar`). Данные — `duplicates_repo`, разметка —
`entities_view`."""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from vera_shared.graph.avatars import get_avatar
from vera_shared.graph.dedup import get_entity_context
from vera_shared.graph.identity import run_identity_analysis, set_suggestion_status
from vera_shared.graph.merge_actions import apply_merge, preview_merge
from vera_shared.graph.merge_candidates import entity_summaries, recommend_keep
from vera_shared.graph.merge_errors import MergeError
from vera_shared.graph.merge_guard import MergeBlocked
from vera_shared.graph.suggestions import reject_pair

from dashboard.csrf import same_origin_or_403
from dashboard.duplicates_repo import load_queue, pair_dossiers
from dashboard.entities_view import pair_view, review_body
from dashboard.render import _render, initials_avatar_svg, owner_or_auth_error

log = logging.getLogger(__name__)


async def _merge_with_report(keep_id: int, drop_id: int, reason: str) -> int | str:
    """Единственный путь слияния из дашборда: защита владельца и узлов личности плюс строка
    журнала (`merge_actions.apply_merge`). Возвращает id записи журнала или код заметки
    (`blocked` / `gone`) для страницы очереди."""
    try:
        return (await apply_merge(keep_id, [drop_id], reason, "dashboard"))["audit_id"]
    except MergeBlocked as e:
        log.warning("слияние отклонено защитой: %s", e)
        return "blocked"
    except MergeError as e:
        log.warning("слияние не выполнено (карточки нет?): %s", e)
        return "gone"


def _after_merge(result: int | str, n: int = 0) -> RedirectResponse:
    key = "merged" if isinstance(result, int) else "notice"
    return RedirectResponse(f"/entities/duplicates?n={n}&{key}={result}", status_code=303)


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
    if (denied := same_origin_or_403(request)) is not None:
        return denied
    if not _analysis["running"]:
        _analysis["running"] = True
        t = asyncio.create_task(_run_analysis_bg())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    return RedirectResponse("/entities/duplicates", status_code=303)


@router.post("/entities/merge-email-dupes")
@router.post("/entities/merge-collisions")
async def entities_bulk_merge_retired(request: Request):
    """Массовые слияния убраны: они шли мимо защиты владельца и журнала. Пары с одинаковым
    email/@username теперь в очереди проверки — каждая объединяется там, с предпросмотром и откатом."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    if (denied := same_origin_or_403(request)) is not None:
        return denied
    return RedirectResponse("/entities/duplicates", status_code=303)


@router.post("/entities/roster-sync")
async def entities_roster_sync(request: Request):
    """Молчуны проектных групп → граф: проксируем команду юзерботу
    (tools-сервер, троттлинг и анти-бан — на его стороне)."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    if (denied := same_origin_or_403(request)) is not None:
        return denied
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
    if (denied := same_origin_or_403(request)) is not None:
        return denied
    status = "rejected" if action == "reject" else "accepted"
    row = await set_suggestion_status(suggestion_id, status)
    if row and action in ("accept_a", "accept_b"):
        keeper = row["entity_a"] if action == "accept_a" else row["entity_b"]
        merged = row["entity_b"] if action == "accept_a" else row["entity_a"]
        result = await _merge_with_report(keeper, merged, f"dashboard: предложение {suggestion_id}")
        return _after_merge(result)
    return RedirectResponse("/entities/duplicates", status_code=303)


@router.get("/entities/duplicates", response_class=HTMLResponse)
async def entity_duplicates_page(request: Request, n: int = 0, sw: int = 0, merged: int | None = None,
                                 notice: str | None = None):
    """Очередь проверки: одна пара за раз; `n` — позиция, `sw=1` — поменять главную карточку."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    queue = await load_queue()
    n = min(max(n, 0), max(len(queue) - 1, 0))
    if not queue:
        return HTMLResponse(_render("entities", review_body(queue, 0, "", None, merged, _analysis, notice)))
    item = queue[n]
    summaries = await entity_summaries([item.a, item.b])
    if len(summaries) < 2:                      # одной из карточек уже нет — пара устарела
        return RedirectResponse(f"/entities/duplicates?n={n}", status_code=303) if n else \
            HTMLResponse(_render("entities", review_body([], 0, "", None, merged, _analysis, notice or "gone")))
    keep = recommend_keep(summaries[item.a], summaries[item.b])
    if sw:
        keep = item.b if keep == item.a else item.a
    drop = item.b if keep == item.a else item.a
    plan = await preview_merge(keep, [drop], "dashboard: предпросмотр очереди")
    dossiers = await pair_dossiers(item.a, item.b)
    return HTMLResponse(_render("entities", review_body(
        queue, n, pair_view(item, n, keep, dossiers, summaries, plan, bool(sw)), keep, merged, _analysis, notice)))


@router.post("/entities/queue/merge")
async def queue_merge(request: Request, a: int = Form(...), b: int = Form(...),  # noqa: B008
                      keep: int = Form(...), n: int = Form(0),  # noqa: B008
                      suggestion_id: int | None = Form(None)):  # noqa: B008
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    if (denied := same_origin_or_403(request)) is not None:
        return denied
    drop = b if keep == a else a
    if keep not in (a, b):
        return RedirectResponse(f"/entities/duplicates?n={n}", status_code=303)
    result = await _merge_with_report(keep, drop, "dashboard: очередь проверки дублей")
    if isinstance(result, int) and suggestion_id:
        await set_suggestion_status(suggestion_id, "accepted")
    return _after_merge(result, n)


@router.post("/entities/queue/reject")
async def queue_reject(request: Request, a: int = Form(...), b: int = Form(...),  # noqa: B008
                       keep: int = Form(0), n: int = Form(0),  # noqa: B008
                       suggestion_id: int | None = Form(None)):  # noqa: B008
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    if (denied := same_origin_or_403(request)) is not None:
        return denied
    if suggestion_id:
        await set_suggestion_status(suggestion_id, "rejected")
    else:
        await reject_pair(a, b, "dashboard: владелец отметил разными людьми")
    return RedirectResponse(f"/entities/duplicates?n={n}", status_code=303)


@router.post("/entities/merge")
async def entity_merge(request: Request,
                       keeper_id: int = Form(...),  # noqa: B008
                       merged_id: int = Form(...)):  # noqa: B008
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    if (denied := same_origin_or_403(request)) is not None:
        return denied
    return _after_merge(await _merge_with_report(keeper_id, merged_id, "dashboard: ручное слияние"))

"""Search proxy (`/search-ui`) — the home page's "спросить Веру" form
posts here; this just forwards to brain-search and renders the answer."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.auth import COOKIE_NAME, require_owner
from dashboard.render import esc, local_dt
from dashboard.source_registry import resolve_source

router = APIRouter()

SOURCES_SHOWN = 5

log = logging.getLogger(__name__)
SEARCH_URL = os.environ.get("SEARCH_URL", "http://brain-search:8000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


def _when(iso: str) -> str:
    try:
        return local_dt(datetime.fromisoformat(iso), "date_human")
    except (TypeError, ValueError):
        return esc(iso)


def sources_html(results: list[dict[str, Any]]) -> str:
    """Пять самых близких событий — то, на чём стоит ответ. Пусто, если
    поиск ничего не вернул (например, расчётный ответ без LLM)."""
    items = []
    for r in results[:SOURCES_SHOWN]:
        if not isinstance(r.get("event_id"), int):
            continue
        src = resolve_source(str(r.get("source") or ""))
        snippet = esc((r.get("content_preview") or "")[:160])
        items.append(
            f'<li><a href="/events/{int(r["event_id"])}">{src.icon} {esc(src.title)}</a>'
            f' <span class="muted small">· {_when(r.get("occurred_at") or "")}</span>'
            f'<div class="muted small">{snippet}</div></li>')
    if not items:
        return ""
    return f'<h4>На чём основан ответ</h4><ul>{"".join(items)}</ul>'


@router.post("/search-ui", response_class=HTMLResponse)
async def search_ui(request: Request, q: str = Form(...)):  # noqa: B008
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return HTMLResponse('<div class="error">Auth required</div>', status_code=401)
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{SEARCH_URL}/search", json={"q": q, "limit": 15},
                             headers={"X-Internal-Secret": INTERNAL_SECRET})
    except httpx.HTTPError as e:
        log.warning("search proxy: brain-search недоступен: %s", e)
        return HTMLResponse(
            '<div class="error">Поиск недоступен: сервис не отвечает</div>',
            status_code=502,
        )
    if r.status_code != 200:
        log.warning("search proxy: brain-search HTTP %s: %s", r.status_code, r.text[:200])
        return HTMLResponse(
            f'<div class="error">Поиск вернул ошибку (HTTP {r.status_code})</div>',
            status_code=502,
        )
    data = r.json()
    # Полный HTML escape ответа + перевод \n в <br>. quote=True закрывает
    # XSS через атрибуты, не только теги.
    answer = esc(data.get("answer", "—")).replace("\n", "<br>")
    provider = esc(data.get("provider") or "—")
    cost = float(data.get("cost_usd", 0.0))
    results = data.get("results", [])
    return HTMLResponse(
        f'<div class="answer">{answer}</div>'
        f'{sources_html(results)}'
        f'<div class="meta">{provider} · ${cost:.4f} · найдено событий: {len(results)}</div>'
    )

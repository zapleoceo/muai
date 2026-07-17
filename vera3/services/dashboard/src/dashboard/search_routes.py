"""Search proxy (`/search-ui`) — the home page's "спросить Веру" form
posts here; this just forwards to brain-search and renders the answer."""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.auth import COOKIE_NAME, require_owner
from dashboard.render import esc

router = APIRouter()

log = logging.getLogger(__name__)
SEARCH_URL = os.environ.get("SEARCH_URL", "http://brain-search:8000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


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
    n = len(data.get("results", []))
    return HTMLResponse(
        f'<div class="answer"><b>Ответ:</b><br>{answer}</div>'
        f'<div class="meta">via {provider}, ${cost:.4f}, {n} событий</div>'
    )

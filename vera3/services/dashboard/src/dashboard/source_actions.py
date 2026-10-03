"""Отключение источника — один общий маршрут на все источники.

Отдельного эндпоинта на источник нет: гашение строки одинаково у всех, а что
именно гасить, знает `source_state`. Так же и подключение остаётся своим у
каждого (OAuth у gmail, код у telegram, пароль у instagram, токен у slack) —
там флоу действительно разные.

GET  /api/sources/{key}/disconnect — подтверждение: что именно погаснет
POST /api/sources/{key}/disconnect — погасить

Подтверждение обязательно: отключение останавливает приём событий, и делать это
одним кликом по ссылке нельзя. Секрет при этом НЕ удаляется — гасится строка,
поэтому шаг обратим.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.auth import COOKIE_NAME, require_owner
from dashboard.csrf import same_origin_or_403
from dashboard.render import esc
from dashboard.source_registry import resolve_source
from dashboard.source_state import can_disconnect, disconnect, state_of
from dashboard.stats import drop_detail_cache
from dashboard.ui.shell import standalone_html

log = logging.getLogger(__name__)
router = APIRouter()


def _page(body: str, *, code: int = 200) -> HTMLResponse:
    return HTMLResponse(standalone_html("Отключить источник", body), status_code=code)


@router.get("/api/sources/{key}/disconnect", response_class=HTMLResponse)
async def disconnect_confirm(key: str, request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    src = resolve_source(key)
    title = f"{src.icon} {esc(src.title)}"
    back = f"/sources/{esc(key)}"
    if not can_disconnect(key):
        return _page(
            f'<h1>{title}</h1><p class="muted">Этот источник из '
            f'дашборда не отключается: секрета в базе у него нет.</p>'
            f'<p><a href="{back}">← к источнику</a></p>', code=400)

    state = await state_of(key)
    if not state.connected:
        return RedirectResponse(f"/sources/{key}", status_code=303)

    return _page(f"""
      <h1>Отключить {title}?</h1>
      <p class="muted">Сейчас подключено: <strong>{esc(state.label)}</strong>.<br>
      {esc(state.affects or "приём событий остановится")}.</p>
      <p class="muted">Уже собранные события <strong>останутся</strong> — отключение
      останавливает приём, а не стирает память. Секрет из базы не удаляется,
      поэтому шаг обратим.</p>
      <form method="post" action="/api/sources/{esc(key)}/disconnect" class="inline">
        <button type="submit" class="danger-solid">Отключить</button>
        <a href="{back}">отмена</a>
      </form>
    """)


@router.post("/api/sources/{key}/disconnect")
async def disconnect_apply(key: str, request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    if (denied := same_origin_or_403(request)) is not None:
        return denied
    if not can_disconnect(key):
        return RedirectResponse(f"/sources/{key}", status_code=303)
    stopped = await disconnect(key)
    drop_detail_cache(key)
    log.info("источник %s отключён из дашборда (погашено строк: %d)", key, stopped)
    return RedirectResponse(f"/sources#open={key}", status_code=303)

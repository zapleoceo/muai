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
from dashboard.source_registry import resolve_source
from dashboard.source_state import can_disconnect, disconnect, state_of
from dashboard.stats import drop_detail_cache

log = logging.getLogger(__name__)
router = APIRouter()


def _page(body: str, *, code: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Отключить источник</title><style>
body{{font-family:-apple-system,sans-serif;background:#0f1115;color:#e4e6eb;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px}}
.box{{background:#1a1d24;padding:36px;border-radius:16px;max-width:480px;width:100%}}
h1{{margin:0 0 14px;font-size:20px}}
button{{padding:11px 22px;border-radius:8px;border:none;background:#c94a4a;color:#fff;
font-weight:600;font-size:15px;cursor:pointer}}
a.cancel{{color:#9aa0a8;margin-left:16px;text-decoration:none;font-size:14px}}
.mute{{color:#9aa0a8;font-size:14px;line-height:1.6}}
b{{color:#e4e6eb}}
</style></head><body><div class="box">{body}</div></body></html>""", status_code=code)


@router.get("/api/sources/{key}/disconnect", response_class=HTMLResponse)
async def disconnect_confirm(key: str, request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    src = resolve_source(key)
    if not can_disconnect(key):
        return _page(
            f'<h1>{src.icon} {src.title}</h1><p class="mute">Этот источник из '
            f'дашборда не отключается: секрета в базе у него нет.</p>'
            f'<p class="mute"><a href="/sources/{key}">← к источнику</a></p>', code=400)

    state = await state_of(key)
    if not state.connected:
        return RedirectResponse(f"/sources/{key}", status_code=303)

    return _page(f"""
      <h1>Отключить {src.icon} {src.title}?</h1>
      <p class="mute">Сейчас подключено: <b>{state.label}</b>.<br>
      {state.affects or "приём событий остановится"}.</p>
      <p class="mute">Уже собранные события <b>останутся</b> — отключение
      останавливает приём, а не стирает память. Секрет из базы не удаляется,
      поэтому шаг обратим.</p>
      <form method="post" action="/api/sources/{key}/disconnect">
        <button type="submit">Отключить</button>
        <a class="cancel" href="/sources/{key}">отмена</a>
      </form>
    """)


@router.post("/api/sources/{key}/disconnect")
async def disconnect_apply(key: str, request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))
    if not can_disconnect(key):
        return RedirectResponse(f"/sources/{key}", status_code=303)
    stopped = await disconnect(key)
    drop_detail_cache(key)
    log.info("источник %s отключён из дашборда (погашено строк: %d)", key, stopped)
    return RedirectResponse(f"/sources/{key}", status_code=303)

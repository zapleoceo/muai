"""Owner-only consent UI for the room MCP OAuth server.

The dashboard already verifies the owner's Telegram session. MCP only sees an
internal call authenticated with INTERNAL_SECRET; it never receives TOKEN_SECRET.
"""
from __future__ import annotations

import html
import os
import re
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dashboard.auth import COOKIE_NAME, require_owner

router = APIRouter()
ORIGIN = "https://dima.veranda.my"
# Referrer-Policy same-origin, не no-referrer: при no-referrer браузер шлёт POST формы
# этой же страницы с `Origin: null`, и проверка Origin ниже отвергала законное согласие
# владельца (08.10.2026, первое подключение dot). same-origin не отдаёт тикет чужим сайтам.
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin",
           "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                      "form-action 'self'; frame-ancestors 'none'"}


_NETLOC = re.compile(r"[A-Za-z0-9.-]+(:[0-9]{1,5})?")


def _callback_origin(redirect_uri: str) -> str | None:
    parsed = urlparse(redirect_uri)
    # netloc уходит в заголовок CSP: только хост[:порт], без ';', пробелов и учётных данных
    if parsed.scheme != "https" or not _NETLOC.fullmatch(parsed.netloc):
        return None
    return f"https://{parsed.netloc}"


def _consent_headers(callback_origin: str) -> dict[str, str]:
    # form-action действует и на редирект после отправки формы: с одним 'self' браузер
    # молча блокировал 303 на callback клиента, согласие тратилось, а код до ChatGPT
    # не доходил (08.10.2026). Разрешён только origin callback этого клиента — MCP
    # уже сверил его с ROOM_OAUTH_REDIRECT_HOSTS при регистрации.
    csp = HEADERS["Content-Security-Policy"].replace(
        "form-action 'self'", f"form-action 'self' {callback_origin}")
    return {**HEADERS, "Content-Security-Policy": csp}


def _ready() -> bool:
    return os.environ.get("ROOM_OAUTH_ENABLED") == "1" and bool(
        os.environ.get("INTERNAL_SECRET"))


async def _internal(method: str, *, ticket: str, actor: str | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
            base_url=os.environ.get("ROOM_OAUTH_INTERNAL_URL", "http://mcp:8000"),
            timeout=8) as client:
        headers = {"X-Internal-Secret": os.environ["INTERNAL_SECRET"]}
        if method == "GET":
            return await client.get("/oauth/internal/consent", params={"ticket": ticket},
                                    headers=headers)
        return await client.post("/oauth/internal/consent",
                                 json={"ticket": ticket, "actor": actor}, headers=headers)


@router.get("/room/oauth/consent")
async def room_oauth_consent(request: Request) -> Response:
    if not _ready():
        return HTMLResponse("Not enabled", status_code=404, headers=HEADERS)
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return HTMLResponse(
            "<h1>Vera owner sign-in required</h1><p><a href='/login'>Sign in</a> "
            "in another tab, then reload this page.</p>", status_code=401,
            headers=HEADERS)
    ticket = request.query_params.get("ticket", "")
    if not ticket:
        return HTMLResponse("Missing authorization request", status_code=400,
                            headers=HEADERS)
    response = await _internal("GET", ticket=ticket)
    if response.status_code != 200:
        return HTMLResponse("Authorization request expired", status_code=400,
                            headers=HEADERS)
    data = response.json()
    callback_origin = _callback_origin(data["redirect_uri"])
    if callback_origin is None:
        return HTMLResponse("Unsupported redirect URI", status_code=400, headers=HEADERS)
    actors = "".join(f"<option value='{html.escape(a)}'>{html.escape(a)}</option>"
                     for a in data["actors"])
    return HTMLResponse(
        "<h1>Allow access to Vera room?</h1>"
        f"<p>Client: {html.escape(data['client_name'] or '(unnamed)')} "
        f"({html.escape(data['client_id'])})</p>"
        f"<p>Redirect: {html.escape(data['redirect_uri'])}</p>"
        "<p>Permissions: room read and write only. No personal memory.</p>"
        "<form method='post'>"
        f"<input type='hidden' name='ticket' value='{html.escape(ticket)}'>"
        f"<label>Agent identity <select name='actor'>{actors}</select></label>"
        "<button type='submit'>Allow room access</button></form>",
        headers=_consent_headers(callback_origin))


@router.post("/room/oauth/consent")
async def room_oauth_approve(request: Request) -> Response:
    if not _ready():
        return HTMLResponse("Not enabled", status_code=404, headers=HEADERS)
    require_owner(request, request.cookies.get(COOKIE_NAME))
    if request.headers.get("origin") != ORIGIN:
        return HTMLResponse("Invalid origin", status_code=403, headers=HEADERS)
    form = await request.form()
    response = await _internal("POST", ticket=str(form.get("ticket", "")),
                               actor=str(form.get("actor", "")))
    if response.status_code != 200:
        return HTMLResponse("Authorization request expired", status_code=400,
                            headers=HEADERS)
    return RedirectResponse(response.json()["redirect_uri"], status_code=303,
                            headers=HEADERS)

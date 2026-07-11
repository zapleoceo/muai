"""Login/logout routes — pairs with the auth/session logic in `auth.py`."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.auth import (
    COOKIE_NAME,
    OWNER_ID,
    get_bot_username,
    issue_session,
    verify_telegram_auth,
)
from dashboard.render import _AUTH_ERROR, _LOGIN_HTML, FAVICON_LINKS

router = APIRouter()


def _set_session_cookie(resp: Response) -> None:
    cookie, ttl = issue_session()
    resp.set_cookie(
        COOKIE_NAME, cookie,
        max_age=ttl, httponly=True, secure=True, samesite="lax", path="/",
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    bot = get_bot_username()
    return HTMLResponse(
        _LOGIN_HTML.replace("__BOT__", bot).replace("__FAVICON__", FAVICON_LINKS)
    )


@router.get("/api/tg_login")
async def tg_login(request: Request):
    data = dict(request.query_params)
    user_id = verify_telegram_auth(data)
    if user_id is None:
        return HTMLResponse(
            _AUTH_ERROR.replace("__MSG__", "Невалидная подпись Telegram")
                       .replace("__FAVICON__", FAVICON_LINKS),
            status_code=403,
        )
    if user_id != OWNER_ID:
        return HTMLResponse(
            _AUTH_ERROR.replace("__MSG__", f"Доступ запрещён для user_id {user_id}")
                       .replace("__FAVICON__", FAVICON_LINKS),
            status_code=403,
        )
    resp = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(resp)
    return resp


@router.get("/api/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/healthz")
async def healthz():
    return {"ok": True, "service": "dashboard"}

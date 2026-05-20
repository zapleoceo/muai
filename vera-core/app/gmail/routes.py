import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.dashboard.auth import require_owner
from app.gmail.oauth import (
    build_auth_url, consume_state, exchange_code,
    fetch_userinfo, refresh_access_token,
)
from app.gmail.store import deactivate, list_accounts, save_account

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/gmail/oauth/start")
async def oauth_start(_=Depends(require_owner)) -> RedirectResponse:
    settings = get_settings()
    if not settings.gmail_client_id or not settings.gmail_client_secret:
        raise HTTPException(500, "Gmail OAuth not configured (GMAIL_CLIENT_ID/SECRET)")
    url, _ = build_auth_url()
    return RedirectResponse(url)


@router.get("/api/gmail/oauth/callback")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    if error:
        return _html(f"<h2>OAuth отменён</h2><p>{error}</p>", 400)
    if not code or not state:
        return _html("<h2>OAuth failed</h2><p>missing code or state</p>", 400)
    if not consume_state(state):
        return _html("<h2>OAuth failed</h2><p>invalid or expired state</p>", 400)

    try:
        tok = await exchange_code(code)
    except Exception as exc:
        log.exception("token exchange failed: %s", exc)
        return _html(f"<h2>Token exchange failed</h2><pre>{exc}</pre>", 500)

    access_token = tok.get("access_token")
    refresh_token = tok.get("refresh_token")
    expires_in = int(tok.get("expires_in", 3500))
    if not access_token or not refresh_token:
        return _html(
            "<h2>Google не вернул refresh_token</h2>"
            "<p>Отвязать в "
            "<a href='https://myaccount.google.com/permissions'>Google Account → Third-party access</a> "
            "и пробовать заново — Google выдаёт refresh_token только при первом consent.</p>",
            400,
        )

    try:
        userinfo = await fetch_userinfo(access_token)
    except Exception as exc:
        log.warning("userinfo failed: %s", exc)
        userinfo = {}

    email = userinfo.get("email") or "unknown"
    expiry = datetime.utcnow() + timedelta(seconds=expires_in - 60)
    await save_account(
        email=email, refresh_token=refresh_token,
        access_token=access_token, access_expiry=expiry,
    )
    log.info("Gmail account connected: %s", email)
    return _html(
        f"<h2>✅ Подключено</h2>"
        f"<p>Аккаунт <code>{email}</code> теперь связан с Vera.</p>"
        f"<p><a href='/'>Вернуться в дашборд</a></p>",
        200,
    )


@router.get("/api/gmail/accounts")
async def api_accounts(_=Depends(require_owner)) -> list[dict]:
    rows = await list_accounts()
    return [
        {
            "id": r["id"], "email": r["email"],
            "is_active": r["is_active"],
            "last_polled_at": r["last_polled_at"].isoformat() if r["last_polled_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.post("/api/gmail/disconnect")
async def api_disconnect(email: str, _=Depends(require_owner)) -> dict:
    ok = await deactivate(email)
    return {"ok": ok}


def _html(body: str, status: int = 200) -> HTMLResponse:
    page = f"""<!doctype html><meta charset=utf-8>
<title>Gmail OAuth</title>
<style>body{{background:#0e1014;color:#e6edf3;font:14px -apple-system,sans-serif;
 max-width:520px;margin:80px auto;padding:32px;border:1px solid #222831;border-radius:8px}}
a{{color:#58a6ff}} pre{{white-space:pre-wrap;color:#7d8590;font-size:12px}}</style>
{body}
"""
    return HTMLResponse(page, status_code=status)

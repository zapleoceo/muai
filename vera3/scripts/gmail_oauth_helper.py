"""Standalone OAuth helper для получения Gmail refresh tokens.

Запускается на сервере, слушает /api/gmail/oauth/callback,
обменивает auth code на refresh_token и сохраняет в БД.

Использование:
    python gmail_oauth_helper.py

После запуска открой:
    https://dima.veranda.my/start
для начала flow.
"""
import asyncio
import logging
import os
import secrets
from datetime import datetime
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("oauth")

CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
BASE_URL = "https://dima.veranda.my"
REDIRECT_URI = f"{BASE_URL}/api/gmail/oauth/callback"

SCOPES = " ".join([
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
])

_states: set[str] = set()
app = FastAPI()


@app.get("/")
async def root():
    return {"ok": True, "service": "vera3-gmail-oauth-helper"}


@app.get("/start")
async def start():
    state = secrets.token_urlsafe(16)
    _states.add(state)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # форсируем refresh_token каждый раз
        "include_granted_scopes": "true",
        "state": state,
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return RedirectResponse(url)


@app.get("/api/gmail/oauth/callback")
async def callback(request: Request):
    qp = dict(request.query_params)
    code = qp.get("code")
    state = qp.get("state")
    error = qp.get("error")

    if error:
        return HTMLResponse(f"<h1>❌ OAuth error</h1><pre>{error}</pre>", status_code=400)
    if not code:
        return HTMLResponse("<h1>❌ no code</h1>", status_code=400)
    if state not in _states:
        return HTMLResponse(f"<h1>❌ unknown state</h1><pre>{state}</pre>", status_code=400)
    _states.discard(state)

    # Exchange code → tokens
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
            },
        )
    if r.status_code != 200:
        return HTMLResponse(
            f"<h1>❌ token exchange failed</h1><pre>{r.status_code}: {r.text[:500]}</pre>",
            status_code=400,
        )
    tokens = r.json()
    refresh = tokens.get("refresh_token")
    access = tokens.get("access_token")
    if not refresh:
        return HTMLResponse(
            "<h1>❌ no refresh_token (user already authorized? logout and try again)</h1>",
            status_code=400,
        )

    # Get email
    async with httpx.AsyncClient(timeout=15) as c:
        r2 = await c.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access}"},
        )
    if r2.status_code != 200:
        return HTMLResponse(
            f"<h1>❌ userinfo failed</h1><pre>{r2.text[:500]}</pre>",
            status_code=400,
        )
    info = r2.json()
    email = info.get("email", "?")

    # Save to Postgres
    try:
        from vera_shared.db.engine import get_session, init_engine
        from vera_shared.db.models_sources import GmailAccountRow
        from vera_shared.tokens.crypto import encrypt
        from sqlalchemy import select, update

        await init_engine()
        refresh_enc = encrypt(refresh)
        async with get_session() as s:
            existing = (await s.execute(
                select(GmailAccountRow).where(GmailAccountRow.email == email)
            )).scalar_one_or_none()
            if existing:
                existing.refresh_token_enc = refresh_enc
                existing.is_active = True
                existing.last_polled_at = None  # перепрокачать с начала
            else:
                s.add(GmailAccountRow(
                    email=email,
                    refresh_token_enc=refresh_enc,
                    is_active=True,
                ))
        log.info("✓ saved %s (refresh starts with %s...)", email, refresh[:10])
    except Exception as e:
        log.exception("DB save failed: %s", e)
        return HTMLResponse(
            f"<h1>❌ DB save failed</h1><pre>{e}</pre>",
            status_code=500,
        )

    return HTMLResponse(f"""
    <html><head><title>✓ Vera 3 — {email}</title>
    <style>
    body {{ font-family: -apple-system, sans-serif; background: #0f1115; color: #e4e6eb;
            display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
    .box {{ background: #1a1d24; padding: 40px; border-radius: 16px; max-width: 500px; text-align: center; }}
    h1 {{ color: #4caf50; font-size: 48px; margin: 0 0 16px; }}
    p {{ font-size: 18px; margin: 12px 0; }}
    .email {{ font-family: monospace; color: #4dabf7; }}
    a {{ color: #4dabf7; }}
    </style></head>
    <body><div class="box">
      <h1>✓</h1>
      <p>Авторизован <span class="email">{email}</span></p>
      <p>Refresh token сохранён в Vera 3.</p>
      <p><a href="/start">→ Авторизовать ещё один аккаунт</a></p>
    </div></body></html>
    """)


@app.get("/list")
async def list_accounts():
    from vera_shared.db.engine import get_session, init_engine
    from vera_shared.db.models_sources import GmailAccountRow
    from sqlalchemy import select
    await init_engine()
    async with get_session() as s:
        rows = (await s.execute(select(GmailAccountRow))).scalars().all()
    return [
        {"id": r.id, "email": r.email, "active": r.is_active,
         "last_polled": r.last_polled_at.isoformat() if r.last_polled_at else None}
        for r in rows
    ]


if __name__ == "__main__":
    log.info("Starting OAuth helper on :8000, callback=%s", REDIRECT_URI)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")

"""Gmail OAuth re-connect — встроен в дашборд.

Зачем: refresh-токены Google в Testing-режиме истекают за 7 дней. Кнопка
«Переподключить» на /sources запускает consent заново — без правки nginx
(redirect_uri /api/gmail/oauth/callback уже идёт на dashboard).

/api/gmail/start    — owner-only, редирект на Google consent (state подписан)
/api/gmail/oauth/callback — обмен code→refresh, сохранить, снять needs_reauth
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.tokens.crypto import encrypt

from dashboard.auth import (
    COOKIE_NAME, issue_oauth_state, require_owner, verify_oauth_state,
)

log = logging.getLogger(__name__)
router = APIRouter()

CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "https://dima.veranda.my")
REDIRECT_URI = f"{BASE_URL}/api/gmail/oauth/callback"
# Least-privilege: Vera только ЧИТАЕТ письма. Один Gmail-scope = одна галка
# на экране Google, меньше шанс случайно снять критичный доступ.
SCOPES = " ".join([
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
])


def _page(title: str, body_html: str, *, code: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>{title}</title><style>
body{{font-family:-apple-system,sans-serif;background:#0f1115;color:#e4e6eb;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.box{{background:#1a1d24;padding:40px;border-radius:16px;max-width:520px;text-align:center}}
.email{{font-family:monospace;color:#4dabf7}} a{{color:#4dabf7}}
.err{{color:#ffaaaa}}</style></head><body><div class="box">{body_html}</div></body></html>""",
        status_code=code)


@router.get("/api/gmail/start")
async def gmail_start(request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))  # raises 401
    if not CLIENT_ID or not CLIENT_SECRET:
        return _page("Ошибка", "<h1 class='err'>GMAIL_CLIENT_ID/SECRET не заданы</h1>",
                     code=500)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",          # форсируем выдачу refresh_token
        "include_granted_scopes": "true",
        "state": issue_oauth_state(),
    }
    return RedirectResponse(
        "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@router.get("/api/gmail/oauth/callback")
async def gmail_callback(request: Request):
    qp = dict(request.query_params)
    if qp.get("error"):
        return _page("OAuth error", f"<h1 class='err'>❌ {qp['error']}</h1>", code=400)
    code = qp.get("code")
    if not code:
        return _page("Ошибка", "<h1 class='err'>❌ нет code</h1>", code=400)
    # Anti-CSRF: state должен быть подписан нами и не протух
    if not verify_oauth_state(qp.get("state")):
        return _page("Ошибка", "<h1 class='err'>❌ невалидный state</h1>", code=403)

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        })
    if r.status_code != 200:
        return _page("Ошибка", f"<h1 class='err'>❌ обмен кода: {r.status_code}</h1>"
                     f"<pre>{r.text[:300]}</pre>", code=400)
    tok = r.json()
    refresh = tok.get("refresh_token")
    access = tok.get("access_token")
    granted = tok.get("scope", "")
    if not refresh:
        return _page("Ошибка",
                     "<h1 class='err'>❌ нет refresh_token</h1>"
                     "<p>Отзови доступ в Google и попробуй снова "
                     "(нужен prompt=consent).</p>", code=400)
    # КРИТИЧНО: без Gmail-scope токен бесполезен (читать письма нельзя).
    # Именно из-за этого demoniwwwe молча не опрашивался. Не сохраняем такой
    # токен — заставляем переподключить с галкой «Чтение писем».
    if "gmail." not in granted:
        return _page("Неполный доступ",
                     "<h1 class='err'>⚠️ Не выдан доступ к Gmail</h1>"
                     "<p>Ты разрешил только профиль/email, но НЕ чтение писем.</p>"
                     "<p>Нажми «Переподключить» снова и <b>оставь все галочки</b> "
                     "(особенно «Просмотр сообщений и настроек почты»).</p>"
                     "<p><a href='/api/gmail/start'>🔁 Переподключить заново</a></p>",
                     code=400)

    async with httpx.AsyncClient(timeout=15) as c:
        r2 = await c.get("https://openidconnect.googleapis.com/v1/userinfo",
                         headers={"Authorization": f"Bearer {access}"})
    if r2.status_code != 200:
        return _page("Ошибка", f"<h1 class='err'>❌ userinfo: {r2.text[:200]}</h1>", code=400)
    email = r2.json().get("email", "?")

    refresh_enc = encrypt(refresh)
    async with get_session() as s:
        existing = (await s.execute(
            select(GmailAccountRow).where(GmailAccountRow.email == email)
        )).scalar_one_or_none()
        if existing:
            existing.refresh_token_enc = refresh_enc
            existing.is_active = True
            existing.needs_reauth = False
            existing.last_error = None
            existing.last_polled_at = None  # перепрокачать новые письма
        else:
            s.add(GmailAccountRow(email=email, refresh_token_enc=refresh_enc,
                                  is_active=True, needs_reauth=False))
    log.info("Gmail re-auth OK: %s", email)
    return _page("Готово",
                 f"<h1>✓</h1><p>Переподключён <span class='email'>{email}</span></p>"
                 "<p><a href='/api/gmail/start'>→ ещё аккаунт</a> &nbsp;·&nbsp; "
                 "<a href='/sources'>← к источникам</a></p>")

"""Gmail OAuth re-connect — встроен в дашборд.

Зачем: refresh-токены Google в Testing-режиме истекают за 7 дней. Кнопка
«Переподключить» на /sources запускает consent заново — без правки nginx
(redirect_uri /api/gmail/oauth/callback уже идёт на dashboard).

/api/gmail/start    — owner-only, редирект на Google consent (state подписан)
/api/gmail/oauth/callback — обмен code→refresh, сохранить, снять needs_reauth

Обмен и сохранение — `gmail_oauth_service`, SQL — `gmail_oauth_repo`; здесь
разбор запроса и страницы ответа.
"""
from __future__ import annotations

from html import escape as _esc

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.auth import (
    COOKIE_NAME,
    issue_oauth_state,
    require_owner,
    verify_oauth_state,
)
from dashboard.gmail_oauth_service import (
    GmailOAuthOutcome,
    complete_gmail_oauth,
    gmail_consent_url,
    gmail_oauth_configured,
)
from dashboard.ui.shell import standalone_html

router = APIRouter()


def _page(title: str, body_html: str, *, code: int = 200) -> HTMLResponse:
    return HTMLResponse(standalone_html(title, body_html), status_code=code)


def _outcome_page(outcome: GmailOAuthOutcome) -> HTMLResponse:
    if outcome.kind == "exchange_failed":
        return _page("Ошибка", f"<h1 class='err'>❌ обмен кода: {outcome.status}</h1>"
                     f"<pre>{_esc(outcome.detail)}</pre>", code=400)
    if outcome.kind == "no_refresh_token":
        return _page("Ошибка",
                     "<h1 class='err'>❌ нет refresh_token</h1>"
                     "<p>Отзови доступ в Google и попробуй снова "
                     "(нужен prompt=consent).</p>", code=400)
    if outcome.kind == "no_gmail_scope":
        return _page("Неполный доступ",
                     "<h1 class='err'>⚠️ Не выдан доступ к Gmail</h1>"
                     "<p>Ты разрешил только профиль/email, но НЕ чтение писем.</p>"
                     "<p>Нажми «Переподключить» снова и <b>оставь все галочки</b> "
                     "(особенно «Просмотр сообщений и настроек почты»).</p>"
                     "<p><a href='/api/gmail/start'>🔁 Переподключить заново</a></p>",
                     code=400)
    if outcome.kind == "userinfo_failed":
        return _page("Ошибка", f"<h1 class='err'>❌ userinfo: {_esc(outcome.detail)}</h1>",
                     code=400)
    return _page("Готово",
                 f"<h1>✓</h1><p>Переподключён <span class='email'>{_esc(outcome.detail)}</span></p>"
                 "<p><a href='/api/gmail/start'>→ ещё аккаунт</a> &nbsp;·&nbsp; "
                 "<a href='/sources'>← к источникам</a></p>")


@router.get("/api/gmail/start")
async def gmail_start(request: Request):
    require_owner(request, request.cookies.get(COOKIE_NAME))  # raises 401
    if not gmail_oauth_configured():
        return _page("Ошибка", "<h1 class='err'>GMAIL_CLIENT_ID/SECRET не заданы</h1>",
                     code=500)
    return RedirectResponse(gmail_consent_url(issue_oauth_state()))


@router.get("/api/gmail/oauth/callback")
async def gmail_callback(request: Request):
    qp = dict(request.query_params)
    if qp.get("error"):
        # Public, no-auth endpoint (Google redirects here) — qp['error'] is
        # fully attacker-controlled via a crafted link. Must escape.
        return _page("OAuth error", f"<h1 class='err'>❌ {_esc(qp['error'])}</h1>", code=400)
    code = qp.get("code")
    if not code:
        return _page("Ошибка", "<h1 class='err'>❌ нет code</h1>", code=400)
    # Anti-CSRF: state должен быть подписан нами и не протух
    if not verify_oauth_state(qp.get("state")):
        return _page("Ошибка", "<h1 class='err'>❌ невалидный state</h1>", code=403)
    return _outcome_page(await complete_gmail_oauth(code))

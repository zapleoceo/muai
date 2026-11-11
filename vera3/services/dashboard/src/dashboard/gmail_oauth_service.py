"""Gmail OAuth без HTTP-слоя: ссылка на consent и обмен code → refresh-токен.

Зачем: refresh-токены Google в Testing-режиме истекают за 7 дней, и кнопка
«Переподключить» на /sources запускает consent заново.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

import httpx
from vera_shared.crypto import encrypt

from dashboard.gmail_oauth_repo import save_gmail_refresh_token

log = logging.getLogger(__name__)

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

GmailOAuthKind = Literal["exchange_failed", "no_refresh_token", "no_gmail_scope",
                         "userinfo_failed", "connected"]


@dataclass(frozen=True)
class GmailOAuthOutcome:
    kind: GmailOAuthKind
    # exchange_failed/userinfo_failed — обрезок ответа Google; connected — email.
    detail: str = ""
    status: int | None = None


def gmail_oauth_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def gmail_consent_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",          # форсируем выдачу refresh_token
        "include_granted_scopes": "true",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


async def complete_gmail_oauth(code: str) -> GmailOAuthOutcome:
    """code → refresh-токен → email → сохранить ящик."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        })
    if r.status_code != 200:
        return GmailOAuthOutcome("exchange_failed", r.text[:300], r.status_code)
    tok = r.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        return GmailOAuthOutcome("no_refresh_token")
    # КРИТИЧНО: без Gmail-scope токен бесполезен (читать письма нельзя).
    # Именно из-за этого demoniwwwe молча не опрашивался. Не сохраняем такой
    # токен — заставляем переподключить с галкой «Чтение писем».
    if "gmail." not in tok.get("scope", ""):
        return GmailOAuthOutcome("no_gmail_scope")

    async with httpx.AsyncClient(timeout=15) as c:
        r2 = await c.get("https://openidconnect.googleapis.com/v1/userinfo",
                         headers={"Authorization": f"Bearer {tok.get('access_token')}"})
    if r2.status_code != 200:
        return GmailOAuthOutcome("userinfo_failed", r2.text[:200], r2.status_code)
    email = r2.json().get("email", "?")

    await save_gmail_refresh_token(email, encrypt(refresh))
    log.info("Gmail re-auth OK: %s", email)
    return GmailOAuthOutcome("connected", email)

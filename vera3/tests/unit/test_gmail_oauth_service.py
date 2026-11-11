"""Gmail re-connect: обмен code → refresh вынесен в `gmail_oauth_service`,
маршрут только разбирает query и рисует страницу по исходу. Google
подменён MockTransport'ом — в сеть тест не ходит."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from dashboard import gmail_oauth_service as svc
from dashboard.app import app
from fastapi.testclient import TestClient

_REAL_CLIENT = httpx.AsyncClient


def _google(token_status: int = 200, token: dict | None = None,
            info_status: int = 200, info: dict | None = None):
    token = token if token is not None else {
        "refresh_token": "r1", "access_token": "a1",
        "scope": "https://www.googleapis.com/auth/gmail.readonly openid"}
    info = info if info is not None else {"email": "box@example.com"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(token_status, json=token)
        return httpx.Response(info_status, json=info)

    def factory(*_a, **kw):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)

    return patch.object(svc.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_connected_saves_encrypted_token():
    save = AsyncMock()
    with _google(), patch.object(svc, "save_gmail_refresh_token", save):
        out = await svc.complete_gmail_oauth("c")
    assert out.kind == "connected" and out.detail == "box@example.com"
    email, enc = save.await_args.args
    assert email == "box@example.com" and enc != "r1"


@pytest.mark.asyncio
@pytest.mark.parametrize(("google", "kind"), [
    ({"token_status": 400}, "exchange_failed"),
    ({"token": {"access_token": "a", "scope": "gmail.readonly"}}, "no_refresh_token"),
    ({"token": {"refresh_token": "r", "access_token": "a", "scope": "openid email"}},
     "no_gmail_scope"),
    ({"info_status": 401}, "userinfo_failed"),
])
async def test_failures_never_save(google, kind):
    save = AsyncMock()
    with _google(**google), patch.object(svc, "save_gmail_refresh_token", save):
        out = await svc.complete_gmail_oauth("c")
    assert out.kind == kind
    save.assert_not_awaited()


def test_consent_url_forces_refresh_token_and_carries_state():
    url = svc.gmail_consent_url("st4te")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "prompt=consent" in url and "access_type=offline" in url and "state=st4te" in url


def test_callback_escapes_error_and_checks_state_before_exchange():
    client = TestClient(app)
    exchange = AsyncMock()
    with patch("dashboard.gmail_oauth.complete_gmail_oauth", exchange):
        r = client.get("/api/gmail/oauth/callback?error=<script>")
        assert r.status_code == 400 and "<script>" not in r.text
        assert client.get("/api/gmail/oauth/callback").status_code == 400
        assert client.get("/api/gmail/oauth/callback?code=c&state=bad").status_code == 403
    exchange.assert_not_awaited()


@pytest.mark.parametrize(("outcome", "status", "needle"), [
    (svc.GmailOAuthOutcome("exchange_failed", "<b>x</b>", 400), 400, "обмен кода: 400"),
    (svc.GmailOAuthOutcome("no_refresh_token"), 400, "нет refresh_token"),
    (svc.GmailOAuthOutcome("no_gmail_scope"), 400, "Не выдан доступ к Gmail"),
    (svc.GmailOAuthOutcome("userinfo_failed", "boom", 401), 400, "userinfo: boom"),
    (svc.GmailOAuthOutcome("connected", "box@example.com"), 200, "box@example.com"),
])
def test_callback_renders_each_outcome(outcome, status, needle):
    client = TestClient(app)
    with patch("dashboard.gmail_oauth.verify_oauth_state", lambda _s: True), \
         patch("dashboard.gmail_oauth.complete_gmail_oauth", AsyncMock(return_value=outcome)):
        r = client.get("/api/gmail/oauth/callback?code=c&state=s")
    assert r.status_code == status and needle in r.text
    assert "<b>x</b>" not in r.text

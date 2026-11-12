"""Выход только POST со страницы дашборда: GET-ссылка или картинка с чужого сайта не разлогинивает."""
from __future__ import annotations

import base64
import os

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def test_get_logout_keeps_the_session_and_goes_home():
    r = _client().get("/api/logout")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_cross_site_post_logout_is_refused():
    r = _client().post("/api/logout", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_same_origin_post_logout_clears_the_cookie():
    r = _client().post("/api/logout", headers={"origin": "http://testserver"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert f"{COOKIE_NAME}=" in r.headers["set-cookie"]

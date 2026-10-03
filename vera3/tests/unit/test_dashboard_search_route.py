"""`/search-ui`: ответ модели рендерится безопасным markdown, а не показывается
литералами `**…**`."""
from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.auth_routes import _set_session_cookie  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.responses import Response  # noqa: E402


def _cookie() -> str:
    resp = Response()
    _set_session_cookie(resp)
    return resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]


def _ask(payload: dict) -> str:
    reply = MagicMock(status_code=200)
    reply.json.return_value = payload
    http = MagicMock()
    http.__aenter__.return_value.post = AsyncMock(return_value=reply)
    with patch("dashboard.search_routes.httpx.AsyncClient", return_value=http):
        r = TestClient(app).post("/search-ui", data={"q": "кто"},
                                 cookies={COOKIE_NAME: _cookie()})
    return r.text


def test_answer_markdown_is_rendered_and_html_is_not():
    html = _ask({"answer": "**Имя** и `код`\n- пункт <script>x</script>", "results": []})
    assert "<strong>Имя</strong>" in html and "<code>код</code>" in html
    assert "<li>" in html and "**" not in html
    assert "<script>" not in html


def test_missing_answer_shows_dash():
    assert "—" in _ask({"results": []})


def test_sources_are_listed_under_the_answer():
    html = _ask({"answer": "ок", "results": [{
        "event_id": 7, "source": "telegram", "occurred_at": "2026-01-01T10:00:00",
        "content_preview": "From: Иван\nChat: Чат (group)\n---\nпривет"}]})
    assert 'href="/events/7"' in html and "Иван · Чат" in html and "привет" in html
    assert "From:" not in html

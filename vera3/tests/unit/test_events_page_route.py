"""Маршрут `/events` действительно рисует фильтры из данных и применяет их.

Хелперы проверены отдельно (`test_events_filters.py`); здесь — проводка:
`get_stats()["sources_all"]` доезжает до выпадающего списка, выбранный
источник уходит в SQL, статус `media_pending` доступен в фильтре.
"""
from __future__ import annotations

import base64
import os

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


def _owner_cookie() -> str:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    header = resp.headers.get("set-cookie", "")
    return header.split(";")[0].split("=", 1)[1] if "=" in header else ""


class _Session:
    def __init__(self):
        self.params: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt, params=None):
        self.params = dict(params or {})
        res = MagicMock()
        res.mappings.return_value.all.return_value = []
        return res


def test_events_page_offers_the_listener_and_filters_by_it():
    session = _Session()
    stats = {"sources_all": [("telegram", 300000), ("slack", 900), ("voice", 113)]}
    with patch("dashboard.events_routes.get_session", lambda: session), \
         patch("dashboard.events_routes.get_stats", AsyncMock(return_value=stats)):
        r = client.get("/events?source=voice&status=media_pending",
                       cookies={COOKIE_NAME: _owner_cookie()})
    assert r.status_code == 200
    assert '<option value="voice" selected>' in r.text
    assert "Разговоры у ноутбука" in r.text
    assert 'value="slack"' in r.text
    assert '<option value="media_pending" selected>' in r.text
    assert session.params["source"] == "voice"
    assert session.params["status"] == "media_pending"

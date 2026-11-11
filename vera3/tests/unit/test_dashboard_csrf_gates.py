"""POST-правки дашборда: сессия владельца без same-origin не меняет ничего (403 до побочного эффекта)."""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.auth_routes import _set_session_cookie  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.responses import Response  # noqa: E402

client = TestClient(app)
CROSS_SITE = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _cookie() -> dict[str, str]:
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


GATED = [
    ("/control/settings", {"x": "1"}, "dashboard.settings_routes.set_control", AsyncMock),
    ("/control/backfill", {"action": "pause"}, "dashboard.progress_routes.set_backfill_paused",
     AsyncMock),
    ("/control/backfill-rate", {"max_per_hour": "5"},
     "dashboard.progress_routes.set_backfill_max_per_hour", AsyncMock),
    ("/search-ui", {"q": "x"}, "dashboard.search_routes.httpx.AsyncClient", MagicMock),
    ("/api/telegram/start", {"phone": "+10000000000"}, "dashboard.telegram_login._prune_flows",
     AsyncMock),
    ("/api/telegram/verify", {"flow_id": "f", "code": "1"},
     "dashboard.telegram_login._prune_flows", AsyncMock),
    ("/api/instagram/start", {"username": "u", "password": "p"},
     "dashboard.instagram_login._prune_flows", MagicMock),
    ("/api/instagram/verify", {"flow_id": "f", "code": "1"},
     "dashboard.instagram_login._prune_flows", MagicMock),
    ("/api/slack/start", {"token": "xoxp-test"}, "dashboard.slack_connect.verify", AsyncMock),
    ("/graph/recluster", {}, "dashboard.graph_routes._recluster_bg", MagicMock),
]


@pytest.mark.parametrize(("path", "form", "effect", "mock_cls"), GATED,
                         ids=[g[0] for g in GATED])
@pytest.mark.parametrize("headers", [CROSS_SITE, {}], ids=["cross-site", "no-origin"])
def test_owner_post_without_same_origin_is_refused_before_any_effect(path, form, effect,
                                                                     mock_cls, headers):
    with patch(effect, new_callable=mock_cls) as fake:
        r = client.post(path, data=form, cookies=_cookie(), headers=headers,
                        follow_redirects=False)
    assert r.status_code == 403
    assert not fake.called


def test_settings_same_origin_saves_and_logs_a_bad_integer(caplog):
    from vera_shared.control import SETTINGS

    numeric = next(s for s in SETTINGS if s.kind != "bool")
    with patch("dashboard.settings_routes.set_control", AsyncMock()) as saved, \
            caplog.at_level(logging.WARNING, logger="dashboard.settings_routes"):
        r = client.post("/control/settings", data={numeric.key: "abc"}, cookies=_cookie(),
                        headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    saved.assert_not_awaited()
    assert numeric.key in caplog.text

    with patch("dashboard.settings_routes.set_control", AsyncMock()) as saved:
        client.post("/control/settings", data={numeric.key: "7"}, cookies=_cookie(),
                    headers=SAME_ORIGIN, follow_redirects=False)
    saved.assert_awaited_once_with(numeric.key, "7")


@pytest.mark.asyncio
async def test_gmail_poll_status_comes_from_the_repository(sqlite_db):
    from dashboard.gmail_accounts_repo import list_gmail_poll_status
    from vera_shared.db.models_sources import GmailAccountRow

    polled = datetime(2026, 11, 1, 12, 0)
    async with sqlite_db() as s:
        s.add_all([GmailAccountRow(email="b@example.com", refresh_token_enc="x"),
                   GmailAccountRow(email="a@example.com", refresh_token_enc="x",
                                   last_polled_at=polled)])
        await s.commit()
    assert await list_gmail_poll_status() == [("b@example.com", None),
                                              ("a@example.com", polled)]

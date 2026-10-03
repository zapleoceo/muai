"""Спойлеры на /sources: ленивая подгрузка, доступ, экранирование, действия, состояние в хэше."""
from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard import source_registry  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.source_state import State  # noqa: E402
from dashboard.sources_script import SOURCES_SCRIPT  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)
CONNECTED = State(connected=True, label="подключён", affects="приём остановится")


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


def _patches(blocks=(), state=CONNECTED):
    return (patch("dashboard.sources_routes.get_sources_overview",
                  AsyncMock(return_value={"telegram": {"total": 5, "c1h": 1, "c24h": 2, "last": None}})),
            patch("dashboard.sources_routes.get_source_detail", AsyncMock(return_value=list(blocks))),
            patch("dashboard.sources_routes.state_of", AsyncMock(return_value=state)))


def test_partial_requires_the_owner():
    r = client.get("/sources/telegram/panel", follow_redirects=False)
    assert r.status_code == 401 and r.text == ""


def test_every_row_is_a_lazy_spoiler_without_a_link_to_another_page():
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        html = client.get("/sources", cookies=_cookie()).text
    for src in source_registry.CATALOG:
        assert f'hx-get="/sources/{src.key}/panel"' in html and f'id="src-{src.key}"' in html
    assert 'hx-trigger="src-open once"' in html and 'aria-expanded="false"' in html
    assert 'href="/sources/' not in html
    assert "skeleton" in html


def test_partial_has_details_and_the_confirm_dialog_disconnect():
    blocks = [{"title": "Подключение", "kind": "rows", "pairs": [("аккаунт", "1")], "hint": ""}]
    p1, p2, p3 = _patches(blocks)
    with p1, p2, p3:
        html = client.get("/sources/telegram/panel", cookies=_cookie()).text
    assert "<html" not in html and "Подключение" in html and "Событий" in html
    assert 'action="/api/sources/telegram/disconnect"' in html and "data-confirm=" in html


def test_partial_escapes_database_strings_and_the_confirm_text():
    evil = "<script>alert(1)</script>"
    blocks = [{"title": evil, "kind": "rows", "pairs": [(evil, evil)], "hint": evil}]
    state = State(connected=True, label=evil, affects='"><img src=x>')
    p1, p2, p3 = _patches(blocks, state)
    with p1, p2, p3:
        html = client.get("/sources/telegram/panel", cookies=_cookie()).text
    assert "<script>alert" not in html and "<img src=x>" not in html


def test_full_page_still_works_for_direct_links_and_shares_the_partial():
    blocks = [{"title": "Разбивка", "kind": "rows", "pairs": [("а", "1")], "hint": ""}]
    p1, p2, p3 = _patches(blocks)
    with p1, p2, p3:
        full = client.get("/sources/telegram", cookies=_cookie()).text
        part = client.get("/sources/telegram/panel", cookies=_cookie()).text
    assert "<h1>" in full and "Разбивка" in full and part in full


def test_disconnect_post_needs_same_origin():
    with patch("dashboard.source_actions.disconnect", AsyncMock(return_value=1)) as run:
        refused = client.post("/api/sources/telegram/disconnect", cookies=_cookie(),
                              headers={"Origin": "https://evil.example"}, follow_redirects=False)
        ok = client.post("/api/sources/telegram/disconnect", cookies=_cookie(),
                         headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert refused.status_code == 403
    assert ok.status_code == 303 and ok.headers["location"] == "/sources#open=telegram"
    run.assert_awaited_once()


def test_open_state_lives_in_the_url_hash():
    for needle in ("open=", "history.replaceState", "htmx.trigger", "aria-expanded"):
        assert needle in SOURCES_SCRIPT


def test_accordion_css_animates_height_and_respects_reduced_motion():
    from dashboard.ui.theme import VERA_CSS
    assert "grid-template-rows:0fr" in VERA_CSS and "grid-template-rows:1fr" in VERA_CSS
    assert ".src-body{transition:none}" in VERA_CSS

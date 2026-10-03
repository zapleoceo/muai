"""Общая шлифовка: подвал с часовым поясом один и мелкий, таблицы скроллятся
внутри контейнера, нет разовых цветовых литералов в разметке страниц,
подтверждение отключения — в теме и с экранированием."""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.render import data_table  # noqa: E402
from dashboard.source_state import State  # noqa: E402
from dashboard.ui.shell import nav, page  # noqa: E402
from dashboard.ui.theme import VERA_CSS  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)
STATS = {"total": 10, "ingest_24h": 1, "pending": 0, "error": 0, "dead": 0, "triage_1h": 1}


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


def _body_without_style_and_script(html: str) -> str:
    html = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    return re.sub(r"<script.*?</script>", "", html, flags=re.S)


class TestShell:
    def test_timezone_footer_is_present_once_and_small(self):
        html = page("home", "<p>x</p>")
        assert html.count('id="tz-note"') == 1
        assert re.search(r"\.tz-note\{[^}]*font-size:\.72rem", VERA_CSS)

    def test_footer_text_is_written_once_by_the_script(self):
        from dashboard.ui.tz import TZ_SCRIPT
        assert "tz.dataset.done" in TZ_SCRIPT and "Время — в вашем часовом поясе" in TZ_SCRIPT

    def test_phone_width_is_declared_and_nav_wraps(self):
        html = page("home", "")
        assert 'name="viewport" content="width=device-width' in html
        assert "nav.top{" in VERA_CSS and "flex-wrap:wrap" in VERA_CSS
        assert nav("home").count("<li>") >= 5

    def test_tables_scroll_inside_their_container(self):
        assert data_table(["a"], "<tr><td>1</td></tr>").startswith('<div class="overflow-auto">')


class TestPagesCarryNoColourLiterals:
    def test_events_and_sources_markup(self):
        with patch("dashboard.events_routes._fetch", AsyncMock(return_value=[{
                "id": 1, "triage_status": "done", "importance": None, "source": "telegram",
                "account": "u", "occurred_at": datetime(2026, 1, 1, 10), "content_text": "x",
                "metadata": {}, "nature": None, "has_emb": False, "request_id": None,
                "model": None, "tokens_in": None, "tokens_out": None, "cost_usd": None}])), \
             patch("dashboard.events_routes.get_stats",
                   AsyncMock(return_value={**STATS, "sources_all": []})):
            events = client.get("/events", cookies=_cookie()).text
        with patch("dashboard.sources_routes.get_sources_overview", AsyncMock(return_value={})), \
             patch("dashboard.sources_routes.state_of",
                   AsyncMock(return_value=State(connected=True, label="ok"))):
            sources = client.get("/sources", cookies=_cookie()).text
        for html in (events, sources):
            markup = _body_without_style_and_script(html)
            assert not re.search(r"#[0-9a-fA-F]{3,6}\b", markup)
            assert 'style="' not in markup


class TestDisconnectConfirmation:
    def _get(self, state):
        with patch("dashboard.source_actions.state_of", AsyncMock(return_value=state)):
            return client.get("/api/sources/slack/disconnect", cookies=_cookie())

    def test_uses_the_shared_theme_and_a_solid_danger_button(self):
        r = self._get(State(True, "Команда", "опрос остановится"))
        assert "/ui/vera.css" in r.text and 'class="danger-solid"' in r.text

    def test_label_from_the_database_is_escaped(self):
        r = self._get(State(True, "<script>alert(1)</script>", "<b>стоп</b>"))
        assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text
        assert "<b>стоп</b>" not in r.text

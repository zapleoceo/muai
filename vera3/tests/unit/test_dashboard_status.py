"""Статусы источников: пороги тишины по источникам, необязательные источники
серые, а не тревожные, «Отключить» — контурная кнопка."""
from __future__ import annotations

import asyncio
import base64
import os

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from datetime import datetime, timedelta  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.health import assess, silent_sources  # noqa: E402
from dashboard.source_freshness import (  # noqa: E402
    EMPTY,
    LIVE,
    NO_POLLING,
    QUIET,
    SILENT,
    freshness_of,
    silence_limit_min,
)
from dashboard.source_registry import BY_KEY, CATALOG  # noqa: E402
from dashboard.source_state import State, disabled_optional, is_off  # noqa: E402
from dashboard.sources_routes import actions, connection_pill  # noqa: E402
from dashboard.sources_view import source_level  # noqa: E402
from dashboard.ui.theme import VERA_CSS  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

NOW = datetime(2026, 10, 3, 12, 0)   # суббота
STATS = {"total": 100, "ingest_24h": 5, "pending": 0, "error": 0, "dead": 0, "triage_1h": 3}
CONNECTED = State(connected=True, label="ok")


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


class TestThresholds:
    @pytest.mark.parametrize("key,limit", [("telegram", 360), ("gmail", 2880), ("slack", 2880)])
    def test_sane_defaults(self, key, limit):
        assert silence_limit_min(BY_KEY[key]) == limit

    def test_saturday_slack_six_hours_is_quiet_not_silent(self):
        assert freshness_of(BY_KEY["slack"], ago(hours=6), NOW).state == QUIET

    def test_slack_two_days_is_silent(self):
        assert freshness_of(BY_KEY["slack"], ago(hours=49), NOW).state == SILENT

    def test_telegram_overnight_gap_is_quiet_but_a_working_day_is_silent(self):
        assert freshness_of(BY_KEY["telegram"], ago(hours=5), NOW).state == QUIET
        assert freshness_of(BY_KEY["telegram"], ago(hours=7), NOW).state == SILENT

    def test_states_at_the_edges(self):
        src = BY_KEY["gmail"]
        assert freshness_of(src, ago(minutes=3), NOW).state == LIVE
        assert freshness_of(src, None, NOW).state == EMPTY
        assert freshness_of(BY_KEY["vera_memory"], None, NOW).state == NO_POLLING

    def test_every_polled_source_has_quiet_room_above_live(self):
        for src in CATALOG:
            if src.live_min is not None:
                assert silence_limit_min(src) > src.live_min


class TestOptionalSources:
    def test_instagram_and_trello_are_optional_with_own_words(self):
        assert BY_KEY["instagram"].optional and BY_KEY["instagram"].off_label == "выключен"
        assert BY_KEY["trello"].optional and BY_KEY["trello"].off_label == "не настроен"
        assert not BY_KEY["slack"].optional

    def test_off_source_is_grey_everywhere(self):
        src, state = BY_KEY["instagram"], State(connected=False, label="сессии нет")
        assert is_off(src, state)
        assert source_level(None, NOW, src, state) is None
        pill = connection_pill(state, src)
        assert "pill off" in pill and "выключен" in pill and "err" not in pill

    def test_trello_says_not_configured(self):
        out = connection_pill(State(connected=False, label="ключ не задан"), BY_KEY["trello"])
        assert "не настроен" in out

    def test_unreadable_state_of_optional_source_is_a_failure_not_a_choice(self):
        broken = State(False, "таблица не создана — миграция не накатана", broken=True)
        assert not is_off(BY_KEY["trello"], broken)
        assert source_level(None, NOW, BY_KEY["trello"], broken) == "err"
        assert "pill err" in connection_pill(broken, BY_KEY["trello"])

    def test_required_source_that_is_down_is_still_red(self):
        state = State(connected=False, label="токена нет")
        assert source_level(None, NOW, BY_KEY["slack"], state) == "err"
        assert "pill err" in connection_pill(state, BY_KEY["slack"])

    def test_connected_optional_source_is_judged_like_any_other(self):
        assert source_level(ago(days=5), NOW, BY_KEY["trello"], CONNECTED) == "err"
        assert not is_off(BY_KEY["trello"], CONNECTED)

    def test_quiet_polled_source_is_grey_not_amber(self):
        assert source_level(ago(hours=6), NOW, BY_KEY["slack"], CONNECTED) is None
        assert source_level(ago(minutes=2), NOW, BY_KEY["slack"], CONNECTED) == "ok"

    def test_health_ignores_disabled_sources(self):
        overview = {"instagram": {"total": 353, "last": ago(days=30)}}
        assert silent_sources(overview, NOW) == ["Instagram"]
        assert silent_sources(overview, NOW, frozenset({"instagram"})) == []
        assert assess(STATS, overview, NOW, frozenset({"instagram"})).level == "ok"

    def test_health_still_flags_a_dead_required_source(self):
        overview = {"slack": {"total": 9, "last": ago(days=4)}}
        assert assess(STATS, overview, NOW, frozenset({"instagram"})).level == "warn"

    def test_saturday_slack_does_not_warn_the_status_line(self):
        overview = {"slack": {"total": 9, "last": ago(hours=6)}}
        assert assess(STATS, overview, NOW).level == "ok"

    def test_disabled_optional_lists_only_unconnected_optional(self):
        async def fake(key):
            return State(connected=(key == "trello"))
        with patch("dashboard.source_state.state_of", side_effect=fake):
            assert asyncio.run(disabled_optional()) == frozenset({"instagram"})


class TestButtonsAndPages:
    def test_disconnect_is_outline_danger_not_solid(self):
        assert 'class="danger"' in actions(BY_KEY["slack"], CONNECTED)
        assert "button.danger,a.danger" in VERA_CSS and "background:transparent" in VERA_CSS

    def _cookie(self):
        from dashboard.auth_routes import _set_session_cookie
        from starlette.responses import Response
        resp = Response()
        _set_session_cookie(resp)
        return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}

    def test_home_status_line_is_calm_when_only_optional_sources_are_off(self):
        overview = {"instagram": {"total": 353, "last": ago(days=30)}}
        with patch("dashboard.home_routes.get_stats", AsyncMock(return_value=STATS)), \
             patch("dashboard.home_routes.get_sources_overview",
                   AsyncMock(return_value=overview)), \
             patch("dashboard.home_routes.disabled_optional",
                   AsyncMock(return_value=frozenset({"instagram"}))):
            r = TestClient(app).get("/", cookies=self._cookie())
        assert "Всё работает" in r.text and "Instagram" not in r.text

    def test_sources_page_shows_off_labels(self):
        async def fake(key):
            return State(connected=False, label="сессии нет") if key in ("instagram", "trello") \
                else State(connected=True, label="ok")
        with patch("dashboard.sources_routes.get_sources_overview", AsyncMock(return_value={})), \
             patch("dashboard.sources_routes.state_of", side_effect=fake):
            r = TestClient(app).get("/sources", cookies=self._cookie())
        assert "pill off" in r.text and "выключен" in r.text and "не настроен" in r.text

"""Новая структура дашборда: меню, главная «поиск вперёд», «Входящее» по дням,
настройки в «Дополнительно», общая оболочка и экранирование."""
from __future__ import annotations

import base64
import os

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from datetime import datetime, timedelta  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.events_view import day_label  # noqa: E402
from dashboard.health import assess, silent_sources  # noqa: E402
from dashboard.search_routes import SOURCES_SHOWN, sources_html  # noqa: E402
from dashboard.ui.shell import nav, standalone_html  # noqa: E402
from dashboard.ui.theme import CSS_URL  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from vera_shared.timeutil import utc_naive_now  # noqa: E402

client = TestClient(app)
NOW = datetime(2026, 10, 3, 12, 0)
STATS = {"total": 12345, "ingest_24h": 77, "pending": 3, "error": 0, "dead": 0,
         "triage_1h": 10}


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    header = resp.headers.get("set-cookie", "")
    return {COOKIE_NAME: header.split(";")[0].split("=", 1)[1]}


class _Session:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.params: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt, params=None):
        self.params = dict(params or {})
        res = MagicMock()
        res.mappings.return_value.all.return_value = self.rows
        return res


def _event(i: int, at: datetime, text: str = "привет", account: str = "ann") -> dict:
    return {"id": i, "triage_status": "done", "importance": 40, "source": "telegram",
            "account": account, "occurred_at": at, "content_text": text, "nature": None,
            "has_emb": False, "request_id": "abcdef012345", "model": "m1",
            "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0001}


def _events(rows: list[dict], query: str = ""):
    session = _Session(rows)
    stats = {"sources_all": [("telegram", len(rows))]}
    with patch("dashboard.events_routes.get_session", lambda: session), \
         patch("dashboard.events_routes.get_stats", AsyncMock(return_value=stats)):
        return client.get(f"/events{query}", cookies=_cookie()), session


class TestShell:
    def test_nav_labels_and_links(self):
        html = nav("home")
        for label in ("Поиск", "Входящее", "Люди", "Источники", "Журнал", "Настройки", "Выйти"):
            assert label in html
        for href in ('href="/"', 'href="/events"', 'href="/graph"',
                     'href="/sources"', 'href="/journal"', 'href="/settings"',
                     'href="/api/logout"'):
            assert href in html
        assert ">log<" not in html and "сущности" not in html

    def test_duplicates_page_highlights_people(self):
        assert 'href="/graph" aria-current="page"' in nav("entities")

    def test_own_theme_pinned_scripts_and_dark_theme(self):
        page = standalone_html("t", "<p>x</p>")
        assert CSS_URL in page and "pico" not in page.lower()
        assert 'integrity="sha384-' in page and 'crossorigin="anonymous"' in page
        assert "htmx.min.js" in page and "@1.9.10" in page
        assert 'data-theme="dark"' in page

    def test_standalone_login_pages_share_the_shell(self):
        from dashboard.telegram_login import _page
        body = _page("Вход", "<h1>Привет</h1>").body.decode()
        assert CSS_URL in body and "<h1>Привет</h1>" in body


class TestHealth:
    def test_all_quiet_is_ok(self):
        assert assess(STATS, {}, NOW).level == "ok"

    def test_failures_warn(self):
        assert assess({**STATS, "error": 2}, {}, NOW).level == "warn"

    def test_stalled_queue_is_error(self):
        h = assess({**STATS, "pending": 900, "triage_1h": 0}, {}, NOW)
        assert h.level == "err"

    def test_silent_polled_source_warns(self):
        overview = {"telegram": {"total": 5, "last": NOW - timedelta(days=3)}}
        assert silent_sources(overview, NOW) == ["Telegram"]
        assert assess(STATS, overview, NOW).level == "warn"


class TestHome:
    def _get(self, stats=STATS):
        with patch("dashboard.home_routes.get_stats", AsyncMock(return_value=stats)), \
             patch("dashboard.home_routes.get_sources_overview", AsyncMock(return_value={})):
            return client.get("/", cookies=_cookie())

    def test_search_first_and_no_pipeline(self):
        r = self._get()
        assert r.status_code == 200
        assert 'hx-post="/search-ui"' in r.text
        assert "Спросить Веру" in r.text
        for gone in ("Live прогресс", "Embeddings", "_progress", "Источники событий"):
            assert gone not in r.text

    def test_status_line_links_to_sources(self):
        r = self._get()
        assert 'href="/sources"' in r.text
        assert "Всё работает" in r.text
        assert "12,345 событий" in r.text and "+77 за сутки" in r.text

    def test_status_line_turns_amber_on_failures(self):
        r = self._get({**STATS, "error": 4})
        assert "dot warn" in r.text and "Есть замечания" in r.text


class TestSearchSources:
    def test_top_five_with_links(self):
        results = [{"event_id": i, "source": "gmail", "occurred_at": "2026-09-01T10:00:00",
                    "content_preview": f"текст {i}"} for i in range(1, 9)]
        html = sources_html(results)
        assert html.count("<li>") == SOURCES_SHOWN
        assert 'href="/events/1"' in html and 'href="/events/6"' not in html

    def test_escapes_snippet_and_survives_bad_date(self):
        html = sources_html([{"event_id": 1, "source": "<b>x</b>", "occurred_at": "<i>",
                              "content_preview": "<script>alert(1)</script>"}])
        assert "<script>" not in html and "<b>x</b>" not in html and "<i>" not in html

    def test_empty_results_render_nothing(self):
        assert sources_html([]) == ""


class TestEvents:
    def test_day_label(self):
        today = NOW.date()
        assert day_label(today, today) == "Сегодня"
        assert day_label(today - timedelta(days=1), today) == "Вчера"
        assert day_label(today - timedelta(days=5), today) == "28.09.2026"

    def test_debug_columns_hidden_by_default(self):
        r, _ = _events([_event(1, utc_naive_now())])
        assert r.status_code == 200
        assert "Сегодня" in r.text
        assert "abcdef01" not in r.text and "m1" not in r.text
        assert 'name="tech"' in r.text and 'name="limit"' not in r.text

    def test_tech_toggle_shows_debug_columns(self):
        r, _ = _events([_event(1, utc_naive_now())], "?tech=1")
        assert "abcdef01" in r.text and "m1" in r.text and "checked" in r.text

    def test_escapes_names_and_text(self):
        evil = "<script>alert(1)</script>"
        r, _ = _events([_event(1, utc_naive_now(), text=evil, account=evil)])
        assert evil not in r.text and "&lt;script&gt;" in r.text

    def test_text_filter_is_bound_and_escaped_for_like(self):
        _, session = _events([], "?q=50%25_x")
        assert session.params["q"] == "%50\\%\\_x%"

    def test_show_more_is_a_keyset_cursor_not_a_growing_limit(self):
        rows = [_event(i, utc_naive_now()) for i in range(3, 0, -1)]
        r, _ = _events(rows, "?limit=2")
        assert "Показать ещё" in r.text and "before=" in r.text
        assert f"_{rows[1]['id']}" in r.text.split("before=")[1].split('"')[0]
        r2, _ = _events(rows[:2], "?limit=2")
        assert "Показать ещё" not in r2.text

    def test_cursor_is_bound_as_a_row_comparison(self):
        _, session = _events([], "?before=2026-10-03T10%3A00%3A00_77")
        assert session.params["before_id"] == 77
        assert session.params["before_at"] == datetime(2026, 10, 3, 10, 0)

    def test_garbage_cursor_is_ignored(self):
        r, session = _events([], "?before=nonsense")
        assert r.status_code == 200 and "before_id" not in session.params

    def test_paging_has_no_upper_boundary_failure(self):
        r, _ = _events([], "?limit=200&before=2026-10-03T10%3A00%3A00_1")
        assert r.status_code == 200

    def test_search_timeout_shows_a_hint(self):
        from sqlalchemy.exc import DBAPIError

        class Slow(_Session):
            async def execute(self, _stmt, params=None):
                raise DBAPIError("select", {}, Exception("canceling statement due to statement timeout"))

        stats = {"sources_all": []}
        with patch("dashboard.events_routes.get_session", lambda: Slow([])),              patch("dashboard.events_routes.get_stats", AsyncMock(return_value=stats)):
            r = client.get("/events?q=abc", cookies=_cookie())
        assert r.status_code == 200 and "Слишком долгий поиск" in r.text

    def test_rows_carry_utc_for_client_side_day_headers(self):
        r, _ = _events([_event(1, datetime(2026, 10, 3, 23, 30))])
        assert 'data-utc="2026-10-03T23:30:00Z"' in r.text and 'class="ev row-link"' in r.text
        assert 'class="day-fb"' in r.text and "tr.day-fb" in r.text


class TestSettingsAndSources:
    def test_operator_settings_live_in_details(self):
        with patch("dashboard.settings_routes.get_settings_values",
                   AsyncMock(return_value={})):
            r = client.get("/settings", cookies=_cookie())
        assert r.status_code == 200
        assert "<details" in r.text and "Дополнительно" in r.text
        assert 'action="/control/settings"' in r.text
        assert "/entities/duplicates" in r.text

    def test_sources_hosts_the_pipeline_block(self):
        from dashboard.source_state import State
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value={})), \
             patch("dashboard.sources_routes.state_of",
                   AsyncMock(return_value=State(connected=True, label="ok"))):
            r = client.get("/sources", cookies=_cookie())
        assert "Конвейер обработки" in r.text
        assert 'hx-get="/_progress"' in r.text and "every 30s" in r.text
        assert "каждые 10с" not in r.text

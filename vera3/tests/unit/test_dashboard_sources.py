"""Страница источников: список строится из каталога, а не из ручной разметки.

Регресс, который тут закрывается: до 2026-08-26 `/sources` набиралась по блоку
HTML на источник, и Trello, добавленный днём раньше, своего блока так и не
получил — был виден только строкой в общем перечне. Теперь новый источник
достаточно объявить в каталоге.
"""
from __future__ import annotations

import base64
import os

# dashboard.app читает секреты на импорте — дефолты ДО импорта.
os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from datetime import datetime, timedelta  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from dashboard import source_registry  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.sources_routes import _freshness, _sources_in_order  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)
NOW = datetime(2026, 8, 26, 12, 0)


def _owner_cookie() -> str:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    header = resp.headers.get("set-cookie", "")
    return header.split(";")[0].split("=", 1)[1] if "=" in header else ""


_OVERVIEW = {
    "telegram": {"total": 414955, "c1h": 12, "c24h": 800,
                 "last": NOW - timedelta(minutes=2)},
    "slack": {"total": 0, "c1h": 0, "c24h": 0, "last": None},
    "trello": {"total": 7, "c1h": 0, "c24h": 7, "last": NOW - timedelta(hours=3)},
    # Источника нет в каталоге — но события от него в базе есть.
    "health": {"total": 100, "c1h": 0, "c24h": 0, "last": None},
}


class TestCatalogue:
    def test_every_catalogue_key_is_unique(self):
        keys = [s.key for s in source_registry.CATALOG]
        assert len(keys) == len(set(keys))

    def test_connectable_sources_declare_both_url_and_label(self):
        """Кнопка без подписи (или наоборот) — молчаливо сломанный UI."""
        for s in source_registry.CATALOG:
            assert bool(s.connect_url) == bool(s.connect_label), s.key

    def test_declared_detail_has_a_provider(self):
        """Каталог обещает подробности — провайдер обязан существовать."""
        from dashboard.source_detail import PROVIDERS
        for s in source_registry.CATALOG:
            if s.detail:
                assert s.detail in PROVIDERS, s.key

    def test_unknown_source_is_not_hidden(self):
        """Скрыть источник без записи — значит соврать про содержимое мозга."""
        ordered = _sources_in_order(_OVERVIEW)
        assert "health" in [s.key for s in ordered]

    def test_catalogue_order_is_preserved_and_extras_go_last(self):
        ordered = [s.key for s in _sources_in_order(_OVERVIEW)]
        assert ordered[:len(source_registry.CATALOG)] == \
            [s.key for s in source_registry.CATALOG]
        assert ordered[-1] == "health"


class TestFreshness:
    def test_live_stream(self):
        src = source_registry.BY_KEY["telegram"]
        assert "живой" in _freshness(NOW - timedelta(minutes=2), NOW, src)

    def test_quiet_stream(self):
        src = source_registry.BY_KEY["telegram"]
        assert "тихо" in _freshness(NOW - timedelta(minutes=30), NOW, src)

    def test_silent_stream(self):
        src = source_registry.BY_KEY["telegram"]
        assert "молчит" in _freshness(NOW - timedelta(hours=5), NOW, src)

    def test_no_data_yet(self):
        src = source_registry.BY_KEY["slack"]
        assert "нет данных" in _freshness(None, NOW, src)

    def test_internal_source_has_no_freshness(self):
        """У vera_memory нет опроса — «молчит 3 дня» было бы ложной тревогой."""
        src = source_registry.BY_KEY["vera_memory"]
        assert _freshness(None, NOW, src) == '<span class="mute">—</span>'


class TestPages:
    def test_index_requires_owner(self):
        r = client.get("/sources", follow_redirects=False)
        assert r.status_code == 303

    def test_detail_requires_owner(self):
        r = client.get("/sources/slack", follow_redirects=False)
        assert r.status_code == 303

    def test_index_lists_every_catalogue_source(self):
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)):
            r = client.get("/sources", cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 200
        for s in source_registry.CATALOG:
            assert s.title in r.text, s.key
        # Источник без записи в каталоге тоже виден.
        assert "health" in r.text

    def test_index_shows_totals_and_connect_action(self):
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)):
            r = client.get("/sources", cookies={COOKIE_NAME: _owner_cookie()})
        assert "414,955" in r.text
        assert "/api/slack/start" in r.text

    def test_detail_renders_blocks_from_the_provider(self):
        blocks = [
            {"title": "Подключение", "kind": "table",
             "headers": ["воркспейс", "состояние"],
             "rows": [["Sintegrum Team", "активен"]], "hint": "подсказка"},
            {"title": "По типу канала", "kind": "rows",
             "pairs": [("channel", "12"), ("im", "3")], "hint": ""},
        ]
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)), \
             patch("dashboard.sources_routes.get_source_detail",
                   AsyncMock(return_value=blocks)):
            r = client.get("/sources/slack", cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 200
        for expected in ("Sintegrum Team", "По типу канала", "channel",
                         "подсказка", "Подключить"):
            assert expected in r.text

    def test_detail_of_source_without_provider_says_so(self):
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)), \
             patch("dashboard.sources_routes.get_source_detail",
                   AsyncMock(return_value=[])):
            r = client.get("/sources/vera_memory",
                           cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 200
        assert "Разбивок для этого источника нет" in r.text

    def test_unknown_source_detail_does_not_500(self):
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value={})), \
             patch("dashboard.sources_routes.get_source_detail",
                   AsyncMock(return_value=[])):
            r = client.get("/sources/nope", cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 200

    @pytest.mark.parametrize("title", ["<script>alert(1)</script>", "a & b"])
    def test_source_title_is_escaped(self, title):
        """chat_title и прочее из БД доезжает до этой страницы как есть."""
        blocks = [{"title": title, "kind": "rows", "pairs": [(title, "1")],
                   "hint": ""}]
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)), \
             patch("dashboard.sources_routes.get_source_detail",
                   AsyncMock(return_value=blocks)):
            r = client.get("/sources/slack", cookies={COOKIE_NAME: _owner_cookie()})
        assert "<script>alert(1)</script>" not in r.text


class TestTableEscaping:
    """chat_title, имя канала и username приходят из БД и подконтрольны чужому
    человеку: любой может назвать чат <script>…</script>. Правило страницы —
    экранируем всё, кроме явно помеченного Html."""

    def _detail(self, blocks):
        return patch("dashboard.sources_routes.get_source_detail",
                     AsyncMock(return_value=blocks))

    def test_table_cell_from_the_db_is_escaped(self):
        evil = '<img src=x onerror=alert(1)>'
        blocks = [{"title": "Топ чатов", "kind": "table",
                   "headers": ["чат", "тип", "событий"],
                   "rows": [[evil, "user", "5"]], "hint": ""}]
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)), self._detail(blocks):
            r = client.get("/sources/telegram", cookies={COOKIE_NAME: _owner_cookie()})
        assert evil not in r.text
        assert "onerror=alert(1)&gt;" in r.text or "&lt;img" in r.text

    def test_html_marked_cell_passes_through(self):
        """Пилюли и <time> обязаны доезжать разметкой, иначе на странице
        будет виден исходник тега вместо состояния."""
        from dashboard.source_detail import Html, state_pill
        blocks = [{"title": "Сессия", "kind": "table",
                   "headers": ["кто", "состояние"],
                   "rows": [["dima", state_pill(True, "активна")]], "hint": ""}]
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)), self._detail(blocks):
            r = client.get("/sources/slack", cookies={COOKIE_NAME: _owner_cookie()})
        assert '<span class="pill ok">активна</span>' in r.text
        assert isinstance(state_pill(True), Html)

    def test_state_pill_is_html_and_dt_is_html(self):
        from datetime import datetime as _dt

        from dashboard.source_detail import Html, dt, state_pill
        assert isinstance(state_pill(False), Html)
        assert isinstance(dt(_dt(2026, 8, 26)), Html)
        assert "<time" in dt(_dt(2026, 8, 26))

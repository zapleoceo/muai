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
from dashboard.source_state import State  # noqa: E402
from dashboard.sources_routes import (  # noqa: E402
    _freshness,
    _sources_in_order,
    actions,
    connection_pill,
)
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


_CONNECTED = State(connected=True, label="Sintegrum Team · dimondra",
                   affects="опрос остановится")
_OFFLINE = State(connected=False, label="токена нет")
_NO_NOTION = State(connected=None)


def _states(**over):
    """По умолчанию всё подключено — тесты страницы про вёрстку, не про доступ."""
    base = {s.key: (_CONNECTED if s.connect_url else _NO_NOTION)
            for s in source_registry.CATALOG}
    base.update(over)
    return AsyncMock(side_effect=lambda key: base.get(key, _NO_NOTION))


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
    @pytest.fixture(autouse=True)
    def _connected(self):
        """Тесты этого класса — про вёрстку, а не про доступ: по умолчанию всё
        подключено. Патч на класс, чтобы новый тест не забыл его и не полез в
        настоящую базу."""
        with patch("dashboard.sources_routes.state_of", _states()):
            yield

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

    def test_index_shows_totals_and_the_action_that_matches_state(self):
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)),              patch("dashboard.sources_routes.state_of",
                   _states(slack=_OFFLINE, telegram=_CONNECTED)):
            r = client.get("/sources", cookies={COOKIE_NAME: _owner_cookie()})
        assert "414,955" in r.text
        # Slack не подключён → ведём на подключение; telegram подключён → на отключение.
        assert "/api/slack/start" in r.text
        assert "/api/sources/telegram/disconnect" in r.text

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
                         "подсказка", "Отключить"):
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

    @pytest.fixture(autouse=True)
    def _connected(self):
        with patch("dashboard.sources_routes.state_of", _states()):
            yield

    def _detail(self, blocks):
        return patch("dashboard.sources_routes.get_source_detail",
                     AsyncMock(return_value=blocks))

    def test_table_cell_from_the_db_is_escaped(self):
        evil = '<img src=x onerror=alert(1)>'
        blocks = [{"title": "Топ чатов", "kind": "table",
                   "headers": ["чат", "тип", "событий"],
                   "rows": [[evil, "user", "5"]], "hint": ""}]
        with patch("dashboard.sources_routes.get_sources_overview",
                   AsyncMock(return_value=_OVERVIEW)),              self._detail(blocks):
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
                   AsyncMock(return_value=_OVERVIEW)),              self._detail(blocks):
            r = client.get("/sources/slack", cookies={COOKIE_NAME: _owner_cookie()})
        assert '<span class="pill ok">активна</span>' in r.text
        assert isinstance(state_pill(True), Html)

    def test_state_pill_is_html_and_dt_is_html(self):
        from datetime import datetime as _dt

        from dashboard.source_detail import Html, dt, state_pill
        assert isinstance(state_pill(False), Html)
        assert isinstance(dt(_dt(2026, 8, 26)), Html)
        assert "<time" in dt(_dt(2026, 8, 26))


class TestAgo:
    """«молчит · 112568 мин» — технически верно и бесполезно: 78 дней
    приходилось делить в голове."""

    @pytest.mark.parametrize("minutes,expect", [
        (0, "0 мин"), (12, "12 мин"), (89, "89 мин"),
        (90, "1 ч"), (200, "3 ч"), (2879, "47 ч"),
        (2880, "2 дн"), (112568, "78 дн"),
    ])
    def test_units_switch_with_scale(self, minutes, expect):
        from dashboard.sources_routes import ago
        assert ago(minutes) == expect

    def test_long_silence_is_reported_in_days_not_minutes(self):
        src = source_registry.BY_KEY["instagram"]
        out = _freshness(NOW - timedelta(days=78), NOW, src)
        assert "78 дн" in out
        assert "мин" not in out


class TestConnectionState:
    """Подключение и свежесть — разные вещи, и раньше их путали: подпись кнопки
    выбиралась по числу событий."""

    def test_connected_shows_what_exactly(self):
        out = connection_pill(_CONNECTED)
        assert "pill ok" in out
        assert "Sintegrum Team" in out

    def test_not_connected_shows_the_reason(self):
        out = connection_pill(_OFFLINE)
        assert "pill err" in out
        assert "токена нет" in out

    def test_source_without_the_notion_shows_a_dash(self):
        assert connection_pill(_NO_NOTION) == '<span class="mute">—</span>'

    def test_state_label_is_escaped(self):
        evil = State(connected=False, label="<script>alert(1)</script>")
        assert "<script>" not in connection_pill(evil)


class TestActions:
    def test_connected_source_offers_disconnect(self):
        out = actions(source_registry.BY_KEY["slack"], _CONNECTED)
        assert "Отключить" in out
        assert "/api/sources/slack/disconnect" in out

    def test_disconnected_source_offers_connect(self):
        out = actions(source_registry.BY_KEY["slack"], _OFFLINE)
        assert "Подключить" in out
        assert "Отключить" not in out
        assert "/api/slack/start" in out

    def test_source_with_events_but_dead_session_offers_connect(self):
        """Instagram с 353 событиями и мёртвой сессией. Раньше подпись
        выбиралась по числу событий и предлагала «Переподключить», как будто
        всё в порядке."""
        out = actions(source_registry.BY_KEY["instagram"],
                      State(False, "сессия неактивна — нужен повторный вход"))
        assert "Подключить" in out
        assert "Отключить" not in out

    def test_source_without_db_secret_cannot_be_disconnected(self):
        """У Trello ключ в infra/.env — гасить из дашборда нечего."""
        out = actions(source_registry.BY_KEY["trello"], State(True, "3 досок"))
        assert "Отключить" not in out

    def test_internal_source_has_no_buttons(self):
        assert actions(source_registry.BY_KEY["vera_memory"], _NO_NOTION) == ""

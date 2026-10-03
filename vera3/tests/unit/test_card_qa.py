"""Правки по живому QA: журнал без «#None», карточка без лишних запросов, один «Разорвать» на
связь, единые даты и значки, подписи дублей, строки-карточки на телефоне."""
from __future__ import annotations

import base64
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.entities_view import option_label  # noqa: E402
from dashboard.event_text import parse_content  # noqa: E402
from dashboard.event_view import header_pairs  # noqa: E402
from dashboard.journal_view import describe_entry, relationship_ids  # noqa: E402
from dashboard.ui.icons import source_icon  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from vera_shared.graph.panel_events import SNIPPET_CHARS, snippet  # noqa: E402

client = TestClient(app)


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


def _row(**kw):
    base = {"id": 1, "client": "sql", "tool": "relationship_retire", "args": {}, "status": "applied",
            "target_kind": "relationship", "target_id": 7, "undo_of": None,
            "created_at": datetime(2026, 10, 4), "before": None, "after": None}
    return SimpleNamespace(**{**base, **kw})


class TestJournalNeverShowsNone:
    def test_entry_without_snapshot_uses_the_relationship_row(self):
        row = _row(args={"relationship_id": 7})
        assert relationship_ids([row]) == [7]
        text = describe_entry(row, {1: "Анна", 2: "Дима"}, {7: (1, "boss_of", 2)})
        assert text == "Связь погашена: Анна — начальник — Дима"

    def test_args_with_subject_and_object_are_enough(self):
        row = _row(tool="relationship_set", args={"subject_id": 1, "object_id": 2, "predicate": "friend_of"})
        assert relationship_ids([row]) == []
        assert "Анна — дружит с — Дима" in describe_entry(row, {1: "Анна", 2: "Дима"})

    def test_unresolvable_entry_names_the_record_not_none(self):
        text = describe_entry(_row(), {}, {})
        assert "None" not in text and "#" not in text and "запись №7" in text

    def test_unknown_person_id_is_not_hash_none(self):
        row = _row(args={"subject_id": 5, "object_id": 6, "predicate": "friend_of"})
        text = describe_entry(row, {}, {})
        assert "None" not in text and "человек №5" in text


class TestCardEndpoints:
    def test_card_can_skip_events_and_ask_for_all_connections(self):
        panel = {"id": 1, "name": "A", "type": "person", "connections": [], "counts": {}}
        mock = AsyncMock(return_value=panel)
        with patch("dashboard.graph_routes.entity_panel", mock), \
             patch("dashboard.graph_routes._owner_fields", AsyncMock(return_value={})):
            client.get("/api/graph/entity/1?events=0&all=1", cookies=_cookie())
        kwargs = mock.await_args.kwargs
        assert kwargs["with_events"] is False and kwargs["connections_limit"] >= 50

    def test_events_endpoint_requires_auth_and_returns_the_list(self):
        assert client.get("/api/graph/entity/1/events").status_code == 401
        with patch("dashboard.graph_routes.entity_aliases", AsyncMock(return_value=[("telegram", "user:1")])), \
             patch("dashboard.graph_routes.recent_events", AsyncMock(return_value=[{"id": 3}])):
            r = client.get("/api/graph/entity/1/events", cookies=_cookie())
        assert r.json() == {"events": [{"id": 3}]}


def test_snippet_ends_with_an_ellipsis_when_cut():
    long = "слово " * 100
    out = snippet("Author: X\n---\n" + long)
    assert out.endswith("…") and len(out) <= SNIPPET_CHARS
    assert snippet("Author: X\n---\nкоротко") == "коротко"


class TestCardScript:
    def test_one_manage_button_per_connection_not_per_role(self):
        from dashboard.graph_script import GRAPH_SCRIPT
        assert GRAPH_SCRIPT.count("data-act=\"manage\"") == 1
        assert "data-act=\"break\"" not in GRAPH_SCRIPT and "'Разорвать: '" in GRAPH_SCRIPT
        assert "и ещё " in GRAPH_SCRIPT and "moreconns" in GRAPH_SCRIPT

    def test_initials_layer_and_faded_labels_and_edge_order(self):
        from dashboard.graph_script import GRAPH_SCRIPT
        assert "initialsUri" in GRAPH_SCRIPT and "e.data('ini')" in GRAPH_SCRIPT
        assert "'text-opacity':0" in GRAPH_SCRIPT                      # подписи приглушённых узлов
        assert "'z-index-compare':'manual'" in GRAPH_SCRIPT           # ребро не перехватывает узел
        assert "panToFree" in GRAPH_SCRIPT and "freeCenter" in GRAPH_SCRIPT
        assert "g-legend-toggle" in GRAPH_SCRIPT and "closest('.g-menu')" in GRAPH_SCRIPT


class TestConsistency:
    def test_one_russian_date_format_for_the_whole_dashboard(self):
        from dashboard.ui.tz import TZ_SCRIPT
        assert "'мая'" in TZ_SCRIPT and "'сегодня'" in TZ_SCRIPT and "window.__fmtDate" in TZ_SCRIPT
        assert "'Jan'" not in TZ_SCRIPT

    def test_icons_are_svg_and_unknown_source_has_a_fallback(self):
        for key in ("telegram", "gmail", "vera_memory", "unknown-x"):
            assert source_icon(key).startswith("<svg") and "<circle" in source_icon("unknown-x")

    def test_nav_has_settings_and_logout_in_one_menu(self):
        from dashboard.ui.shell import nav
        html = nav("home")
        menu = html[html.index('<details class="menu">'):]
        assert 'href="/settings"' in menu and 'href="/api/logout"' in menu

    def test_mobile_events_are_stacked_cards_with_text_first(self):
        from dashboard.ui.theme import VERA_CSS
        assert 'grid-template-areas:"text text text text" "src who time st"' in VERA_CSS
        assert "table.data thead{display:none}" in VERA_CSS and "min-height:44px" in VERA_CSS


class TestDuplicatesLabels:
    def test_option_shows_what_tells_namesakes_apart(self):
        dossier = {"username": "dima_k", "msg_count": 120, "top_chats": [("Рабочий чат", 50)]}
        label = option_label({"id": 5, "name": "Дима"}, dossier)
        assert label == "Дима · @dima_k · 120 сообщ. · Рабочий чат · #5"
        assert option_label({"id": 6, "name": "Дима"}, None) == "Дима · #6"


class TestEventPageHeader:
    def test_who_is_not_repeated_next_to_from(self):
        parsed = parse_content("From: Анна <a@x.example>\nTo: Я <me@x.example>\n---\nтекст")
        labels = [k for k, _ in header_pairs(parsed, "Анна", {})]
        assert "Кто" not in labels and "От" in labels

    def test_known_people_link_to_the_graph_card(self):
        parsed = parse_content("From: Анна <a@x.example>\n---\nтекст")
        pairs = dict(header_pairs(parsed, "Анна", {"a@x.example": 42}))
        assert 'href="/graph#person=42"' in pairs["От"]

    def test_names_from_the_database_are_escaped(self):
        parsed = parse_content("From: <script>x</script> <a@x.example>\n---\nтекст")
        assert "<script>" not in dict(header_pairs(parsed, "", {}))["От"]


@pytest.mark.parametrize("path", ["/journal", "/graph", "/sources"])
def test_pages_still_need_login(path):
    assert client.get(path, follow_redirects=False).status_code in (303, 401, 403)

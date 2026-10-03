"""Правки связей и журнал в дашборде: доступ, защита от чужих сайтов, перевод ошибок,
экранирование журнала, структура новой темы и графа."""
from __future__ import annotations

import base64
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.journal_view import describe_entry, entry_html  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from vera_shared.graph.edit import GraphEditError  # noqa: E402
from vera_shared.journal.undo import UndoRefused  # noqa: E402

client = TestClient(app)
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
BREAK = "/api/graph/connection/break"
BODY = {"entity_a": 1, "entity_b": 2, "predicate": "spouse_of", "rel_ids": [7]}


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


class TestAccess:
    @pytest.mark.parametrize("url,body", [
        (BREAK, BODY), ("/api/graph/connection/reject", {"entity_a": 1, "entity_b": 2}),
        ("/api/journal/undo", {"audit_ids": [1]})])
    def test_edits_need_the_owner(self, url, body):
        assert client.post(url, json=body, headers=SAME_ORIGIN).status_code == 401

    def test_cross_site_post_is_refused_even_for_the_owner(self):
        with patch("dashboard.connection_routes.break_role", AsyncMock()) as run:
            r = client.post(BREAK, json=BODY, cookies=_cookie(),
                            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        run.assert_not_awaited()

    def test_request_without_origin_information_is_refused(self):
        with patch("dashboard.connection_routes.break_role", AsyncMock()) as run:
            assert client.post(BREAK, json=BODY, cookies=_cookie()).status_code == 403
        run.assert_not_awaited()

    def test_forwarded_host_is_not_trusted(self):
        with patch("dashboard.connection_routes.break_role", AsyncMock()) as run:
            r = client.post(BREAK, json=BODY, cookies=_cookie(),
                            headers={"Origin": "https://evil.example",
                                     "X-Forwarded-Host": "evil.example"})
        assert r.status_code == 403
        run.assert_not_awaited()

    def test_origin_matching_the_host_is_accepted(self):
        with patch("dashboard.connection_routes.break_role", AsyncMock(return_value=[5])):
            r = client.post(BREAK, json=BODY, cookies=_cookie(),
                            headers={"Origin": "http://testserver"})
        assert r.status_code == 200

    def test_journal_page_needs_the_owner(self):
        assert client.get("/journal", follow_redirects=False).status_code in (303, 401, 403)


class TestEdits:
    def test_break_passes_the_pair_and_labels_the_client_dashboard(self):
        with patch("dashboard.connection_routes.break_role",
                   AsyncMock(return_value=[11, 12])) as run:
            r = client.post(BREAK, json={**BODY, "rel_ids": [7, 8]}, cookies=_cookie(),
                            headers=SAME_ORIGIN)
        assert r.json() == {"ok": True, "audit_ids": [11, 12]}
        run.assert_awaited_once_with(1, 2, "spouse_of", [7, 8], "dashboard")

    def test_domain_errors_become_409(self):
        with patch("dashboard.connection_routes.break_role",
                   AsyncMock(side_effect=GraphEditError("already retired"))):
            r = client.post(BREAK, json=BODY, cookies=_cookie(), headers=SAME_ORIGIN)
        assert r.status_code == 409 and "already retired" in r.json()["error"]

    def test_bad_ids_are_rejected_before_any_work(self):
        r = client.post(BREAK, json={"entity_a": 0, "entity_b": 2, "predicate": "x", "rel_ids": []},
                        cookies=_cookie(), headers=SAME_ORIGIN)
        assert r.status_code == 422

    def test_reject_reports_nothing_to_undo_when_already_rejected(self):
        with patch("dashboard.connection_routes.reject_inferred", AsyncMock(return_value=None)):
            r = client.post("/api/graph/connection/reject", json={"entity_a": 1, "entity_b": 2},
                            cookies=_cookie(), headers=SAME_ORIGIN)
        assert r.json() == {"ok": True, "audit_ids": []}

    def test_undo_runs_each_entry_and_reports_a_refusal(self):
        undo = AsyncMock(side_effect=[{"ok": True}, UndoRefused("already undone")])
        with patch("dashboard.connection_routes.undo_entry", undo):
            r = client.post("/api/journal/undo", json={"audit_ids": [3, 4]},
                            cookies=_cookie(), headers=SAME_ORIGIN)
        assert r.status_code == 409
        assert r.json()["undone"] == [3] and "already undone" in r.json()["error"]
        assert [c.args[1] for c in undo.await_args_list] == [3, 4]
        assert all(c.kwargs["force"] is False for c in undo.await_args_list)


def _row(**kw):
    base = {"id": 9, "client": "dashboard", "tool": "relationship_retire", "args": {}, "status": "applied",
                "target_kind": "relationship", "target_id": 7, "undo_of": None,
                "created_at": datetime(2026, 10, 4, 12, 0),
                "before": {"subject_entity_id": 1, "object_entity_id": 2, "predicate": "spouse_of",
                        "is_current": True}, "after": None}
    return SimpleNamespace(**{**base, **kw})


class TestJournalView:
    def test_entry_names_the_pair_and_offers_undo(self):
        html = entry_html(_row(), {1: "Дима", 2: "Ли"})
        assert "Связь погашена: Дима — супруг(а) — Ли" in html
        assert 'data-undo="9"' in html and "дашборд" in html

    def test_names_from_the_database_are_escaped(self):
        html = entry_html(_row(), {1: "<script>alert(1)</script>", 2: '"><img src=x>'})
        assert "<script>alert" not in html and "<img" not in html and "&lt;script&gt;" in html

    def test_undone_and_undo_entries_have_no_button(self):
        assert "data-undo" not in entry_html(_row(status="undone"), {})
        assert "data-undo" not in entry_html(_row(undo_of=3, tool="undo"), {})
        assert "Откат записи №3" in describe_entry(_row(undo_of=3), {})

    def test_suppression_entry_reads_as_a_sentence(self):
        row = _row(target_kind="suppression", tool="connection_suppress", before=None,
                   after={"entity_a": 1, "entity_b": 2})
        assert describe_entry(row, {1: "А", 2: "Б"}) == "«Работает с» отвергнуто: А — Б"

    def test_page_renders_empty_state_and_rows(self):
        with patch("dashboard.journal_routes.recent_rows", AsyncMock(return_value=[])):
            assert "Правок пока нет" in client.get("/journal", cookies=_cookie()).text
        names = {1: SimpleNamespace(name="Дима"), 2: SimpleNamespace(name="Ли")}
        with patch("dashboard.journal_routes.recent_rows", AsyncMock(return_value=[_row()])), \
             patch("dashboard.journal_routes.entity_cards", AsyncMock(return_value=names)):
            html = client.get("/journal", cookies=_cookie()).text
        assert "Связь погашена: Дима" in html and "/api/journal/undo" in html


UI = Path(__file__).resolve().parents[2] / "services" / "dashboard" / "src" / "dashboard"


class TestDesignSystemStructure:
    def test_assets_are_served_with_a_forever_cache(self):
        from dashboard.ui.theme import CSS_URL, JS_URL
        css, js = client.get(CSS_URL), client.get(JS_URL)
        assert css.status_code == js.status_code == 200
        assert "immutable" in css.headers["cache-control"]
        assert "prefers-reduced-motion" in css.text and "dialog.dlg" in css.text
        assert "window.VeraUI" in js.text

    def test_pico_is_gone_and_scripts_are_pinned_with_sri(self):
        html = client.get("/graph", cookies=_cookie()).text
        assert "pico" not in html.lower()
        for src in ("htmx.org@1.9.10", "cytoscape@3.30.2"):
            tag = next(t for t in html.split("<script") if src in t)
            assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag

    def test_graph_canvas_is_resize_aware_and_the_panel_floats(self):
        from dashboard.graph_css import GRAPH_CSS
        from dashboard.graph_script import GRAPH_SCRIPT
        assert "new ResizeObserver(() => cy.resize())" in GRAPH_SCRIPT
        assert ".g-panel{position:absolute" in GRAPH_CSS
        assert ".g-canvas{position:absolute;inset:0" in GRAPH_CSS

    def test_connection_actions_are_wired_to_endpoints_and_use_the_dialog(self):
        from dashboard.graph_script import GRAPH_SCRIPT
        for needle in ("/api/graph/connection/break", "/api/graph/connection/reject",
                       "/api/journal/undo", "VeraUI.choose", "вернуть можно в журнале",
                       "label: 'Вернуть'", "seq !== panelSeq"):
            assert needle in GRAPH_SCRIPT

    def test_new_modules_stay_small(self):
        names = ["ui/css_base.py", "ui/css_controls.py", "ui/css_pages.py", "ui/js_core.py",
                 "graph_css.py", "graph_script_core.py", "graph_script_panel.py",
                 "graph_script_ui.py", "graph_script_connections.py", "journal_view.py"]
        for name in names:
            assert len((UI / name).read_text(encoding="utf-8").splitlines()) <= 200, name

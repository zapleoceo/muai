"""Очередь проверки дублей: данные, одна пара за раз, три кнопки, доступ и CSRF, прежние POST.
Имена и фразы синтетические."""
from __future__ import annotations

import asyncio
import base64
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard import duplicates_repo  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.duplicates_repo import QueueItem  # noqa: E402
from dashboard.entities_cards import order_pair, person_card  # noqa: E402
from dashboard.entities_view import pair_view, review_body  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app, follow_redirects=False)
IDLE = {"running": False, "last": None}
SAME = {"Sec-Fetch-Site": "same-origin"}


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


def _dossier(i: int, name: str, msgs: int, **over) -> dict:
    base = {"entity_id": i, "name": name, "username": f"user{i}", "tg_id": i, "msg_count": msgs,
            "dom_project": "itstep", "top_chats": [("Чат", msgs)], "samples": ["привет всем"]}
    base.update(over)
    return base


def _summary(i: int, name: str = "X", **over) -> dict:
    base = {"id": i, "name": name, "type": "person", "username": None, "email": f"p{i}@corp.example",
            "last_seen_at": "2026-10-01 10:00:00", "aliases": 2, "relationships": 3, "groups": 1}
    base.update(over)
    return base


def _item(**over) -> QueueItem:
    base = {"a": 1, "b": 2, "reason": "одно имя и чаты", "kind": "suggestion", "confidence": 0.92,
            "suggestion_id": 5, "verdict": "same"}
    base.update(over)
    return QueueItem(**base)


class TestQueueData:
    def _load(self, suggestions, email, username, decided=frozenset()):
        with patch.object(duplicates_repo, "list_pending_suggestions", AsyncMock(return_value=suggestions)), \
             patch.object(duplicates_repo, "find_email_collisions", AsyncMock(return_value=email)), \
             patch.object(duplicates_repo, "find_alias_collisions", AsyncMock(return_value=username)), \
             patch.object(duplicates_repo, "list_decided_pairs", AsyncMock(return_value=set(decided))):
            return asyncio.run(duplicates_repo.load_queue())

    def test_queue_holds_suggestions_then_exact_pairs_without_name_dump(self):
        sg = [{"id": 5, "entity_a": 1, "entity_b": 2, "verdict": "same", "confidence": 0.9, "reason": "r"}]
        email = [{"email": "e", "size": 2, "candidates": [{"id": 3}, {"id": 4}]},
                 {"email": "big", "size": 3, "candidates": [{"id": 5}, {"id": 6}, {"id": 7}]}]
        username = [{"username": "u", "size": 2, "candidates": [{"id": 2}, {"id": 1}]},
                    {"username": "v", "size": 2, "candidates": [{"id": 8}, {"id": 9}]}]
        queue = self._load(sg, email, username)
        assert [(i.kind, i.a, i.b) for i in queue] == [("suggestion", 1, 2), ("email", 3, 4), ("username", 8, 9)]
        assert queue[1].reason == "Одинаковый рабочий email"

    def test_decided_pairs_do_not_return(self):
        username = [{"username": "v", "size": 2, "candidates": [{"id": 8}, {"id": 9}]}]
        assert self._load([], [], username, decided={(8, 9)}) == []


class TestCards:
    def test_pair_puts_the_more_active_card_on_the_left(self):
        d = {1: _dossier(1, "A", 3), 2: _dossier(2, "B", 90)}
        assert order_pair(1, 2, d) == (2, 1)

    def test_person_card_is_escaped(self):
        html = person_card(1, _dossier(1, "<script>x</script>", 1, samples=["<img src=x onerror=1>"],
                                       username="<b>"))
        assert "<script>" not in html and "<img src=x" not in html and "<b>" not in html

    def test_card_without_dossier_still_renders(self):
        assert "#7" in person_card(7, None)


class TestReviewView:
    def _html(self, queue_len=41, n=2, **kw):
        item = _item(**kw)
        pair = pair_view(item, n, 1, {1: _dossier(1, "Анна", 5), 2: _dossier(2, "Anna", 2)},
                         {1: _summary(1), 2: _summary(2)}, {"counts": {"relationships_moved": 4,
                          "entity_aliases_moved": 2}, "blockers": []}, False)
        return review_body([item] * queue_len, n, pair, 1, None, IDLE)

    def test_one_pair_with_progress_three_big_buttons_and_shortcuts(self):
        html = self._html()
        assert "3 из 41" in html
        for label in ("Это один человек", "Разные люди", "Пропустить"):
            assert label in html
        assert html.count('data-key="') == 3 and "<kbd>Y</kbd>" in html
        assert "Почему предложено" in html and "92%" in html
        assert "<b>4</b> связей" in html and "<b>2</b> алиасов" in html

    def test_exact_match_pair_has_no_confidence_but_a_reason(self):
        html = self._html(kind="email", confidence=None, suggestion_id=None, verdict=None,
                          reason="Одинаковый рабочий email")
        assert "Одинаковый рабочий email" in html and "%" not in html.split("Почему предложено")[1][:60]

    def test_blockers_hide_the_promise_to_move(self):
        item = _item()
        html = pair_view(item, 0, 1, {}, {1: _summary(1), 2: _summary(2)},
                         {"counts": None, "blockers": ["entity 1 (Дима) is the owner"]}, False)
        assert "is the owner" in html and "Переедет" not in html

    def test_empty_queue_points_to_the_card_search(self):
        html = review_body([], 0, "", None, None, IDLE)
        assert "Очередь пуста" in html and "Это тот же человек" in html

    def test_names_from_the_database_are_escaped(self):
        item = _item(reason="<script>alert(1)</script>")
        html = review_body([item], 0, pair_view(item, 0, 1, {}, {}, {"counts": {}, "blockers": []}, False), 1, None, IDLE)
        assert "<script>alert" not in html

    def test_merged_notice_offers_undo(self):
        assert 'data-undo="77"' in review_body([_item()], 0, "", 1, 77, IDLE)


class TestRoutes:
    def test_page_requires_auth(self):
        assert client.get("/entities/duplicates").status_code in (401, 403)

    def test_page_renders_one_pair(self):
        with patch("dashboard.entities_routes.load_queue", AsyncMock(return_value=[_item(), _item(a=3, b=4)])), \
             patch("dashboard.entities_routes.entity_summaries",
                   AsyncMock(return_value={1: _summary(1, "A"), 2: _summary(2, "B", relationships=0, aliases=0, groups=0)})), \
             patch("dashboard.entities_routes.preview_merge",
                   AsyncMock(return_value={"counts": {}, "blockers": [], "would_be_refused": False})) as prev, \
             patch("dashboard.entities_routes.pair_dossiers",
                   AsyncMock(return_value={1: _dossier(1, "A", 9), 2: _dossier(2, "B", 1)})):
            r = client.get("/entities/duplicates", cookies=_cookie())
        assert r.status_code == 200 and "1 из 2" in r.text and "/ui/vera.css" in r.text
        assert prev.await_args.args[:2] == (1, [2])          # главная — у кого больше данных

    def test_swap_switches_the_main_card(self):
        with patch("dashboard.entities_routes.load_queue", AsyncMock(return_value=[_item()])), \
             patch("dashboard.entities_routes.entity_summaries",
                   AsyncMock(return_value={1: _summary(1), 2: _summary(2)})), \
             patch("dashboard.entities_routes.preview_merge",
                   AsyncMock(return_value={"counts": {}, "blockers": []})) as prev, \
             patch("dashboard.entities_routes.pair_dossiers", AsyncMock(return_value={})):
            client.get("/entities/duplicates?sw=1", cookies=_cookie())
        assert prev.await_args.args[:2] == (2, [1])

    def test_preview_in_the_queue_writes_nothing(self):
        with patch("dashboard.entities_routes.load_queue", AsyncMock(return_value=[_item()])), \
             patch("dashboard.entities_routes.entity_summaries",
                   AsyncMock(return_value={1: _summary(1), 2: _summary(2)})), \
             patch("dashboard.entities_routes.preview_merge",
                   AsyncMock(return_value={"counts": {}, "blockers": []})), \
             patch("dashboard.entities_routes.pair_dossiers", AsyncMock(return_value={})), \
             patch("dashboard.entities_routes.apply_merge", AsyncMock()) as apply:
            client.get("/entities/duplicates", cookies=_cookie())
        apply.assert_not_awaited()

    def test_queue_merge_goes_through_the_journaled_path_as_dashboard(self):
        with patch("dashboard.entities_routes.apply_merge", AsyncMock(return_value={"audit_id": 31})) as apply, \
             patch("dashboard.entities_routes.set_suggestion_status", AsyncMock()) as status:
            r = client.post("/entities/queue/merge", data={"a": 1, "b": 2, "keep": 2, "n": 4, "suggestion_id": 5},
                            cookies=_cookie(), headers=SAME)
        assert r.status_code == 303 and r.headers["location"] == "/entities/duplicates?n=4&merged=31"
        assert apply.await_args.args[:2] == (2, [1]) and apply.await_args.args[3] == "dashboard"
        status.assert_awaited_once_with(5, "accepted")

    def test_queue_posts_need_the_owner_and_same_origin(self):
        for path in ("/entities/queue/merge", "/entities/queue/reject"):
            body = {"a": 1, "b": 2, "keep": 1}
            assert client.post(path, data=body, headers=SAME).status_code in (401, 403)
            with patch("dashboard.entities_routes.apply_merge", AsyncMock()) as apply, \
                 patch("dashboard.entities_routes.reject_pair", AsyncMock()) as reject:
                r = client.post(path, data=body, cookies=_cookie(),
                                headers={"Origin": "https://evil.example"})
            assert r.status_code == 403
            apply.assert_not_awaited()
            reject.assert_not_awaited()

    def test_reject_marks_suggestion_or_records_the_exact_pair(self):
        with patch("dashboard.entities_routes.set_suggestion_status", AsyncMock()) as status, \
             patch("dashboard.entities_routes.reject_pair", AsyncMock()) as pair:
            client.post("/entities/queue/reject", data={"a": 1, "b": 2, "keep": 1, "n": 0, "suggestion_id": 5},
                        cookies=_cookie(), headers=SAME)
            status.assert_awaited_once_with(5, "rejected")
            client.post("/entities/queue/reject", data={"a": 3, "b": 4, "keep": 3, "n": 0},
                        cookies=_cookie(), headers=SAME)
            assert pair.await_args.args[:2] == (3, 4)

    def test_legacy_merge_endpoint_still_merges_but_needs_same_origin(self):
        merge = AsyncMock()
        with patch("dashboard.entities_routes._merge_with_report", merge):
            refused = client.post("/entities/merge", data={"keeper_id": 1, "merged_id": 2}, cookies=_cookie())
            ok = client.post("/entities/merge", data={"keeper_id": 1, "merged_id": 2},
                             cookies=_cookie(), headers=SAME)
        assert refused.status_code == 403 and ok.status_code == 303
        assert merge.await_args.args[:2] == (1, 2)

    def test_suggestion_accept_merges_and_reject_does_not(self):
        row = {"id": 5, "entity_a": 1, "entity_b": 2}
        merge = AsyncMock()
        with patch("dashboard.entities_routes._merge_with_report", merge), \
             patch("dashboard.entities_routes.set_suggestion_status", AsyncMock(return_value=row)):
            client.post("/entities/suggestion", data={"suggestion_id": 5, "action": "accept_b"},
                        cookies=_cookie(), headers=SAME)
            assert merge.await_args.args[:2] == (2, 1)
            merge.reset_mock()
            client.post("/entities/suggestion", data={"suggestion_id": 5, "action": "reject"},
                        cookies=_cookie(), headers=SAME)
            merge.assert_not_awaited()

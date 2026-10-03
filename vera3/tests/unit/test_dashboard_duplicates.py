"""Страница дублей: слой данных отдельно от маршрута, парные карточки,
понятные подписи, опасные кнопки с подтверждением, те же POST-эндпоинты.
Имена и фразы синтетические."""
from __future__ import annotations

import asyncio
import base64
import os
import re
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard import duplicates_repo  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.duplicates_repo import DuplicatesData  # noqa: E402
from dashboard.entities_cards import order_pair, person_card  # noqa: E402
from dashboard.entities_view import (  # noqa: E402
    duplicates_body,
    exact_section,
    name_section,
    vera_section,
)
from dashboard.ui.theme import DUPES_CSS  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app, follow_redirects=False)
IDLE = {"running": False, "last": None}


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


def _suggestion(sid=5, a=1, b=2, verdict="same", conf=0.92) -> dict:
    return {"id": sid, "entity_a": a, "entity_b": b, "verdict": verdict,
            "confidence": conf, "reason": "одно имя и чаты"}


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


class TestRepository:
    def _load(self, collisions, groups, suggestions):
        dossiers = AsyncMock(return_value={})
        with patch.object(duplicates_repo, "find_alias_collisions",
                          AsyncMock(return_value=collisions)), \
             patch.object(duplicates_repo, "find_duplicates_by_name",
                          AsyncMock(return_value=groups)), \
             patch.object(duplicates_repo, "list_pending_suggestions",
                          AsyncMock(return_value=suggestions)), \
             patch.object(duplicates_repo, "find_email_collisions", AsyncMock(return_value=[])), \
             patch.object(duplicates_repo, "find_email_collisions", AsyncMock(return_value=[])), \
             patch.object(duplicates_repo, "get_entity_dossiers", dossiers):
            data = asyncio.run(duplicates_repo.load_duplicates())
        return data, dossiers

    def test_collects_every_shown_candidate_into_one_batch_call(self):
        collisions = [{"username": "u", "size": 2,
                       "candidates": [{"id": 1, "name": "A", "type": "person"},
                                      {"id": 2, "name": "B", "type": "channel"}]}]
        group = {"normalized": "дима", "size": 8,
                 "candidates": [{"id": 10 + i, "name": "Дима"} for i in range(8)]}
        data, dossiers = self._load(collisions, [group], [_suggestion(a=3, b=4)])
        dossiers.assert_awaited_once()
        asked = dossiers.await_args.args[0]
        assert {1, 2, 3, 4} <= set(asked) and len(asked) == len(set(asked))
        assert len([i for i in asked if i >= 10]) == duplicates_repo.CANDIDATES_PER_GROUP

    def test_name_groups_are_trimmed_and_counted(self):
        groups = [{"normalized": f"n{i}", "size": 9,
                   "candidates": [{"id": i * 100 + k, "name": "x"} for k in range(9)]}
                  for i in range(40)]
        data, _ = self._load([], groups, [])
        assert data.name_groups_total == 40
        assert len(data.name_groups) == duplicates_repo.NAME_GROUPS_SHOWN
        assert len(data.name_groups[0]["candidates"]) == duplicates_repo.CANDIDATES_PER_GROUP
        assert data.name_groups[0]["hidden"] == 9 - duplicates_repo.CANDIDATES_PER_GROUP

    def test_empty(self):
        data, _ = self._load([], [], [])
        assert data == DuplicatesData(dossiers={})


class TestCards:
    def test_pair_puts_the_more_active_card_on_the_left(self):
        d = {1: _dossier(1, "A", 3), 2: _dossier(2, "B", 90)}
        assert order_pair(1, 2, d) == (2, 1)
        assert order_pair(2, 1, d) == (2, 1)

    def test_person_card_is_escaped(self):
        html = person_card(1, _dossier(1, "<script>x</script>", 1, samples=["<img src=x onerror=1>"],
                                       username="<b>"))
        assert "<script>" not in html and "<img src=x" not in html and "<b>" not in html

    def test_card_without_dossier_still_renders(self):
        assert "#7" in person_card(7, None)


class TestVeraSection:
    def _html(self, a=1, b=2):
        d = {1: _dossier(1, "Маша", 5), 2: _dossier(2, "Maria", 50)}
        data = DuplicatesData(suggestions=[_suggestion(a=a, b=b)], dossiers=d)
        return vera_section(data, IDLE)

    def test_both_people_side_by_side_with_plain_actions(self):
        html = self._html()
        assert html.count('class="person"') == 2 and 'class="pair"' in html
        assert "Объединить" in html and "Это разные люди" in html
        assert "92%" in html and "Скорее всего один человек" in html

    def test_keep_left_maps_to_the_right_suggestion_action(self):
        # entity 2 активнее и стоит слева → «Объединить» оставляет entity_b
        html = self._html()
        assert html.index('value="accept_b"') < html.index('value="accept_a"')
        assert html.index('value="accept_b"') < html.index("Оставить правую")

    def test_unsure_verdict_is_worded_as_doubt(self):
        d = {1: _dossier(1, "A", 1), 2: _dossier(2, "B", 1)}
        html = vera_section(DuplicatesData(
            suggestions=[_suggestion(verdict="unsure", conf=0.6)], dossiers=d), IDLE)
        assert "Возможно, один человек" in html

    def test_pair_with_missing_dossier_is_skipped(self):
        data = DuplicatesData(suggestions=[_suggestion()], dossiers={1: _dossier(1, "A", 1)})
        assert 'class="pair"' not in vera_section(data, IDLE)

    def test_running_analysis_and_empty_states(self):
        busy = vera_section(DuplicatesData(), {"running": True, "last": None})
        assert "анализирует" in busy and "Пока предложений нет" not in busy
        assert "Пока предложений нет" in vera_section(DuplicatesData(), IDLE)

    def test_last_run_is_in_plain_words(self):
        last = {"judged": 9, "same": 2, "unsure": 3, "different": 4}
        html = vera_section(DuplicatesData(), {"running": False, "last": last})
        assert "один человек 2" in html and "разные 4" in html


class TestExactAndNameSections:
    def _collision(self, size=2):
        cands = [{"id": i, "name": f"Имя{i}", "type": "person"} for i in range(1, size + 1)]
        d = {c["id"]: _dossier(c["id"], c["name"], c["id"]) for c in cands}
        return DuplicatesData(collisions=[{"username": "u", "candidates": cands, "size": size}],
                              dossiers=d, email_pairs=3)

    def test_bulk_buttons_are_dangerous_and_ask_for_confirmation(self):
        html = exact_section(self._collision())
        assert html.count("danger-solid") == 2 and html.count("data-confirm=") >= 2
        assert "по email (3)" in html and "по @username (1)" in html and "Объединить 3 пар" in html
        assert 'action="/entities/merge-email-dupes"' in html
        assert 'action="/entities/merge-collisions"' in html

    def test_two_profile_group_is_a_pair_with_hidden_ids(self):
        html = exact_section(self._collision())
        assert 'class="pair"' in html
        assert 'name="keeper_id" value="2"' in html and 'name="merged_id" value="1"' in html

    def test_bigger_group_uses_selects_with_plain_labels(self):
        html = exact_section(self._collision(3))
        assert "cand-grid" in html and "Оставить" in html and "Влить в неё" in html

    def test_name_groups_are_collapsed_with_a_warning(self):
        cands = [{"id": 1, "name": "Дима"}, {"id": 2, "name": "Дима"}]
        data = DuplicatesData(
            name_groups=[{"normalized": "дима", "size": 2, "hidden": 0, "candidates": cands}],
            name_groups_total=1,
            dossiers={1: _dossier(1, "Дима", 1), 2: _dossier(2, "Дима", 1)})
        html = name_section(data)
        assert html.startswith("<details") and "Почти всегда это разные люди" in html
        assert "Совпадения только по имени: групп 1" in html

    def test_nothing_to_show(self):
        assert "нет" in name_section(DuplicatesData())

    def test_no_english_jargon_in_visible_text(self):
        data = self._collision()
        full = _text(duplicates_body(DuplicatesData(
            suggestions=[_suggestion()], collisions=data.collisions,
            dossiers={**data.dossiers, 1: _dossier(1, "A", 1), 2: _dossier(2, "B", 2)}), IDLE, None))
        for word in ("keeper", "merged", "merge", "Keeper"):
            assert word not in full

    def test_untrusted_group_labels_cannot_inject(self):
        cands = [{"id": 1, "name": '"><script>x</script>'}, {"id": 2, "name": "B"}]
        data = DuplicatesData(
            name_groups=[{"normalized": "<i>", "size": 2, "hidden": 0, "candidates": cands}],
            name_groups_total=1, dossiers={})
        html = name_section(data)
        assert "<script>" not in html and "<i>" not in html


class TestRoutes:
    def test_page_requires_auth(self):
        assert client.get("/entities/duplicates").status_code in (401, 403)

    def test_page_renders_with_theme_and_notice(self):
        data = DuplicatesData(suggestions=[_suggestion()],
                              dossiers={1: _dossier(1, "A", 1), 2: _dossier(2, "B", 2)})
        with patch("dashboard.entities_routes.load_duplicates", AsyncMock(return_value=data)):
            r = client.get("/entities/duplicates?merged=2", cookies=_cookie())
        assert r.status_code == 200 and "/ui/vera.css" in r.text
        assert ".pair{" in DUPES_CSS and "Объединено" in r.text
        assert 'style="' not in r.text

    def test_merge_endpoint_still_merges_through_the_report_path(self):
        merge = AsyncMock()
        with patch("dashboard.entities_routes._merge_with_report", merge):
            r = client.post("/entities/merge", data={"keeper_id": 1, "merged_id": 2},
                            cookies=_cookie())
        assert r.status_code == 303 and r.headers["location"].endswith("merged=2")
        assert merge.await_args.args[:2] == (1, 2)

    def test_merge_requires_auth(self):
        with patch("dashboard.entities_routes._merge_with_report", AsyncMock()) as merge:
            r = client.post("/entities/merge", data={"keeper_id": 1, "merged_id": 2})
        assert r.status_code in (401, 403) and not merge.await_count

    def test_suggestion_accept_merges_and_reject_does_not(self):
        row = {"id": 5, "entity_a": 1, "entity_b": 2}
        merge = AsyncMock()
        with patch("dashboard.entities_routes._merge_with_report", merge), \
             patch("dashboard.entities_routes.set_suggestion_status", AsyncMock(return_value=row)):
            client.post("/entities/suggestion", data={"suggestion_id": 5, "action": "accept_b"},
                        cookies=_cookie())
            assert merge.await_args.args[:2] == (2, 1)
            merge.reset_mock()
            client.post("/entities/suggestion", data={"suggestion_id": 5, "action": "reject"},
                        cookies=_cookie())
            merge.assert_not_awaited()

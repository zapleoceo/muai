"""Боковая панель «Людей»: данные карточки (`entity_panel`), счётчик дублей,
эндпоинт `/api/graph/entity/{id}` и новая вёрстка страницы. Данные синтетические."""
from __future__ import annotations

import base64
import os
from datetime import datetime
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from vera_shared.db.engine import (
    Base,  # noqa: E402
    get_session,  # noqa: E402
)
from vera_shared.db.models import EventRow  # noqa: E402
from vera_shared.graph import panel, repo, suggestions  # noqa: E402

client = TestClient(app)


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@pytest_asyncio.fixture
async def db(tmp_path):
    import vera_shared.db.engine as engine_mod
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None
    engine = await engine_mod.init_engine(f"sqlite+aiosqlite:///{tmp_path / 'g.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None


async def _person() -> tuple[int, int]:
    a = await repo.upsert_entity(type="person", name="Иван Тестов", source="telegram",
                                 identifier="user:42",
                                 attributes={"username": "ivan_t", "tg_id": "42",
                                             "employer": "Рога и копыта", "role": "аналитик"})
    org = await repo.upsert_entity(type="organization", name="Рога и копыта", source="s",
                                   identifier="org")
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=org,
                                   predicate="works_at", confidence=0.9)
    return a, org


async def _event(i: int, sender: str, at: datetime, text: str) -> None:
    async with get_session() as s:
        s.add(EventRow(source="telegram", source_event_id=str(i), content_text=text,
                       occurred_at=at, metadata_={"sender_id": sender}))


@pytest.mark.asyncio
async def test_panel_carries_profile_aliases_counts_and_relationships(db):
    a, org = await _person()
    p = await panel.entity_panel(a)
    assert p["name"] == "Иван Тестов" and p["username"] == "ivan_t" and p["tg_id"] == "42"
    assert p["profile"] == ["Рога и копыта", "аналитик"]
    assert {"source": "telegram", "identifier": "user:42"} in p["aliases"]
    assert p["counts"] == {"relationships": 1, "groups": 0, "members": 0}
    conn = p["connections"][0]
    assert conn["main"]["predicate"] == "works_at" and conn["other_id"] == org
    assert conn["other_name"] == "Рога и копыта" and conn["main"]["direction"] == "out"
    assert "relationships" not in p


@pytest.mark.asyncio
async def test_panel_raw_flag_adds_the_individual_records(db):
    a, org = await _person()
    rel = (await panel.entity_panel(a, raw=True))["relationships"][0]
    assert rel["predicate"] == "works_at" and rel["other_id"] == org and rel["direction"] == "out"


@pytest.mark.asyncio
async def test_panel_lists_latest_events_newest_first_with_body_snippet(db):
    a, _ = await _person()
    await _event(1, "42", datetime(2026, 1, 1), "Author: X\nFrom: X\n---\nстарое")
    await _event(2, "42", datetime(2026, 2, 1), "Author: X\nFrom: X\n---\nновое  сообщение")
    await _event(3, "99", datetime(2026, 3, 1), "чужое")
    events = (await panel.entity_panel(a))["events"]
    assert [e["snippet"] for e in events] == ["новое сообщение", "старое"]
    assert all(e["source"] == "telegram" for e in events)


@pytest.mark.asyncio
async def test_panel_event_snippet_is_limited(db):
    a, _ = await _person()
    await _event(1, "42", datetime(2026, 1, 1), "---\n" + "слово " * 100)
    assert len((await panel.entity_panel(a))["events"][0]["snippet"]) <= panel.SNIPPET_CHARS


@pytest.mark.asyncio
async def test_panel_for_unknown_entity_is_none(db):
    assert await panel.entity_panel(12345) is None


@pytest.mark.asyncio
async def test_pending_suggestions_are_counted(db):
    a, org = await _person()
    c = await repo.upsert_entity(type="person", name="Другой", source="s", identifier="c")
    assert await suggestions.count_pending_suggestions() == 0
    assert await suggestions.propose_merge(a, c, confidence=0.9, reason="тест")
    assert await suggestions.count_pending_suggestions() == 1


class TestEntityEndpoint:
    def test_requires_auth(self):
        assert client.get("/api/graph/entity/1").status_code == 401

    def test_returns_panel_with_russian_labels(self):
        data = {"id": 1, "name": "A", "type": "person", "connections": [
            {"other_id": 2, "other_name": "B", "other_type": "person", "weight": 0.7,
             "main": {"predicate": "boss_of", "direction": "in", "weight": 0.7},
             "also": [{"predicate": "client_of", "direction": "out", "weight": 0.5}]}],
            "relationships": [{"predicate": "works_at", "direction": "out", "other_id": 2,
                               "other_name": "B", "other_type": "org"}], "events": []}
        mock = AsyncMock(return_value=data)
        with patch("dashboard.graph_routes.entity_panel", mock):
            r = client.get("/api/graph/entity/1?raw=true", cookies=_cookie())
        assert r.status_code == 200
        mock.assert_awaited_once_with(1, raw=True)
        conn = r.json()["connections"][0]
        assert conn["main"]["label"] == "начальник"            # смотрящий — подчинённый
        assert conn["also"][0]["label"] == "поставщик"         # он клиент — другой поставщик
        assert r.json()["relationships"][0]["label"] == "работает в"

    def test_unknown_entity_is_404(self):
        with patch("dashboard.graph_routes.entity_panel", AsyncMock(return_value=None)):
            assert client.get("/api/graph/entity/9", cookies=_cookie()).status_code == 404


class TestGraphPage:
    def _page(self, pending):
        with patch("dashboard.graph_routes.count_pending_suggestions",
                   AsyncMock(return_value=pending)):
            return client.get("/graph", cookies=_cookie()).text

    def test_search_has_a_button_and_filters_are_collapsed(self):
        html = self._page(0)
        assert 'id="g-searchform"' in html and ">Найти</button>" in html
        assert "<summary>Фильтры</summary>" in html
        assert html.index("<summary>Фильтры</summary>") < html.index('id="g-mindeg"')
        assert html.index("<summary>Фильтры</summary>") < html.index("Раскрасить по темам")

    def test_side_panel_replaces_the_info_line_on_tap(self):
        html = self._page(0)
        assert 'id="g-panel"' in html and "Открыть в Telegram" in html
        assert "/api/graph/entity/" in html

    def test_duplicates_link_shows_pending_count(self):
        html = self._page(7)
        assert 'href="/entities/duplicates"' in html and "Дубли (7)" in html

    def test_duplicates_link_survives_unreadable_count(self):
        from sqlalchemy.exc import OperationalError
        with patch("dashboard.graph_routes.count_pending_suggestions",
                   AsyncMock(side_effect=OperationalError("s", {}, Exception("x")))):
            html = client.get("/graph", cookies=_cookie()).text
        assert ">Дубли</a>" in html

    def test_no_leftover_inline_colour_literals_in_markup(self):
        from dashboard.graph_page import graph_body
        body = graph_body(["works_at"], 0)
        markup = body.split("<script>")[0].replace(
            body[body.index("<style>"):body.index("</style>")], "")
        assert "style=" not in markup and "#" not in markup.replace("href=\"#\"", "")

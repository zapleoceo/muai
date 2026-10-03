"""«Указать связь»: каноническая форма в базе, ручной вес, журнал и откат, эндпоинт."""
from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.manual_roles_ui import ROLE_TEXT  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from vera_shared.db.engine import get_session  # noqa: E402
from vera_shared.db.models_graph import RelationshipRow  # noqa: E402
from vera_shared.db.models_mcp import McpAuditRow  # noqa: E402
from vera_shared.graph import connections, repo  # noqa: E402
from vera_shared.graph.edit import GraphEditError  # noqa: E402
from vera_shared.graph.manual_roles import MANUAL_ROLES, set_manual_role  # noqa: E402
from vera_shared.journal.undo import undo_entry  # noqa: E402

client = TestClient(app)
URL = "/api/graph/connection/set"
SAME = {"Sec-Fetch-Site": "same-origin"}


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


async def person(name: str, ident: str) -> int:
    return await repo.upsert_entity(type="person", name=name, source="telegram",
                                    identifier=f"user:{ident}")


async def rows() -> list[RelationshipRow]:
    async with get_session() as s:
        return list((await s.execute(select(RelationshipRow))).scalars())


def test_labels_cover_exactly_the_server_roles():
    assert set(ROLE_TEXT) == set(MANUAL_ROLES)


@pytest.mark.asyncio
async def test_stored_in_canonical_form_as_a_manual_row(sqlite_db):
    me, boss = await person("Дима Владелец", "1"), await person("Анна Начальникова", "2")
    await set_manual_role(boss, me, "x_boss_of_y", "dashboard")
    (row,) = await rows()
    assert (row.subject_entity_id, row.predicate, row.object_entity_id) == (boss, "boss_of", me)
    assert row.derived_from_event_id is None and row.confidence == 1.0 and row.is_current
    out = await connections.entity_connections(me)
    assert out[0]["main"]["predicate"] == "boss_of" and out[0]["main"]["manual"] is True
    assert out[0]["main"]["direction"] == "in" and out[0]["weight"] == 1.0


@pytest.mark.asyncio
async def test_direction_and_symmetric_ordering(sqlite_db):
    me, other = await person("Я Владелец", "1"), await person("Друг Тестов", "2")
    await set_manual_role(other, me, "y_boss_of_x", "dashboard")      # я — начальник другого
    await set_manual_role(other, me, "friends", "dashboard")
    by_predicate = {r.predicate: r for r in await rows()}
    assert (by_predicate["boss_of"].subject_entity_id, by_predicate["boss_of"].object_entity_id) == (me, other)
    friend = by_predicate["friend_of"]
    assert friend.subject_entity_id < friend.object_entity_id


@pytest.mark.asyncio
async def test_extracted_row_becomes_manual_and_undo_restores_it(sqlite_db):
    a, b = await person("Анна Тестова", "1"), await person("Борис Тестов", "2")
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b, predicate="client_of",
                                   confidence=0.55, derived_from_event_id=None)
    async with get_session() as s:
        row = (await s.execute(select(RelationshipRow))).scalar_one()
        row.derived_from_event_id = None
    audit_id = await set_manual_role(a, b, "x_client_of_y", "dashboard")
    (row,) = await rows()
    assert row.confidence == 1.0
    async with get_session() as s:
        entry = (await s.execute(select(McpAuditRow).where(McpAuditRow.id == audit_id))).scalar_one()
        assert (entry.client, entry.tool, entry.before["confidence"]) == ("dashboard", "relationship_set", 0.55)
        await undo_entry(s, audit_id, "dashboard", force=False)
    (row,) = await rows()
    assert row.confidence == 0.55 and row.is_current


@pytest.mark.asyncio
async def test_undo_of_a_new_manual_role_retires_it(sqlite_db):
    a, b = await person("Анна Тестова", "1"), await person("Борис Тестов", "2")
    audit_id = await set_manual_role(a, b, "spouses", "dashboard")
    async with get_session() as s:
        await undo_entry(s, audit_id, "dashboard", force=False)
    (row,) = await rows()
    assert row.is_current is False


@pytest.mark.asyncio
async def test_rejects_unknown_role_and_self_pair(sqlite_db):
    a = await person("Анна Тестова", "1")
    with pytest.raises(GraphEditError, match="unknown role"):
        await set_manual_role(a, a, "nonsense", "dashboard")
    with pytest.raises(GraphEditError, match="differ"):
        await set_manual_role(a, a, "friends", "dashboard")


class TestEndpoint:
    def test_needs_the_owner(self):
        r = client.post(URL, json={"entity_a": 1, "entity_b": 2, "role": "friends"}, headers=SAME)
        assert r.status_code == 401

    def test_cross_site_is_refused(self):
        with patch("dashboard.connection_routes.set_manual_role", AsyncMock()) as run:
            r = client.post(URL, json={"entity_a": 1, "entity_b": 2, "role": "friends"},
                            cookies=_cookie(), headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        run.assert_not_awaited()

    def test_passes_pair_and_role_labelled_dashboard(self):
        with patch("dashboard.connection_routes.set_manual_role", AsyncMock(return_value=9)) as run:
            r = client.post(URL, json={"entity_a": 1, "entity_b": 2, "role": "friends"},
                            cookies=_cookie(), headers=SAME)
        assert r.json() == {"ok": True, "audit_ids": [9]}
        run.assert_awaited_once_with(1, 2, "friends", "dashboard")

    def test_domain_error_is_409(self):
        with patch("dashboard.connection_routes.set_manual_role",
                   AsyncMock(side_effect=GraphEditError("unknown role"))):
            r = client.post(URL, json={"entity_a": 1, "entity_b": 2, "role": "x"},
                            cookies=_cookie(), headers=SAME)
        assert r.status_code == 409

    def test_card_script_offers_the_action(self):
        from dashboard.graph_script import GRAPH_SCRIPT
        for needle in ("Указать связь", "/api/graph/connection/set", "VeraUI.choose",
                       "вернуть можно в журнале"):
            assert needle in GRAPH_SCRIPT

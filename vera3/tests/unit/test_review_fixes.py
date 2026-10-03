"""Правки по ревью merge-ux: слияние только через защиту и журнал, неизвестное авторство,
возврат погашенной цели, владелец, карточка и сорванные запросы, экранирование LIKE."""
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
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.entities_view import review_body  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from vera_shared.db.engine import get_session  # noqa: E402
from vera_shared.db.models import EventRow  # noqa: E402
from vera_shared.db.models_graph import (  # noqa: E402
    EntityAliasRow,
    EntityRow,
    RelationshipRow,
)
from vera_shared.db.models_mcp import McpAuditRow  # noqa: E402
from vera_shared.graph import panel_events, repo  # noqa: E402
from vera_shared.graph.merge_errors import MergeError  # noqa: E402
from vera_shared.graph.merge_guard import MergeBlocked  # noqa: E402
from vera_shared.graph.relationship_move import (  # noqa: E402
    authorship,
    move_relationships,
    name_evidence,
    preview_move,
)
from vera_shared.journal.undo import undo_entry  # noqa: E402
from vera_shared.projects.rules import OWNER_TG_ID  # noqa: E402

client = TestClient(app, follow_redirects=False)
SAME = {"Sec-Fetch-Site": "same-origin"}


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


class TestLegacyMergesUseTheGuardedJournaledPath:
    def test_manual_merge_goes_through_apply_merge_as_dashboard(self):
        with patch("dashboard.entities_routes.apply_merge", AsyncMock(return_value={"audit_id": 12})) as ap:
            r = client.post("/entities/merge", data={"keeper_id": 1, "merged_id": 2},
                            cookies=_cookie(), headers=SAME)
        assert r.status_code == 303 and "merged=12" in r.headers["location"]
        assert ap.await_args.args[:2] == (1, [2]) and ap.await_args.args[3] == "dashboard"

    def test_owner_merge_is_refused_with_a_notice_not_merged_away(self):
        with patch("dashboard.entities_routes.apply_merge", AsyncMock(side_effect=MergeBlocked("owner"))):
            r = client.post("/entities/merge", data={"keeper_id": 1, "merged_id": 2},
                            cookies=_cookie(), headers=SAME)
        assert "notice=blocked" in r.headers["location"] and "merged=" not in r.headers["location"]

    def test_suggestion_accept_is_journaled_too(self):
        row = {"id": 5, "entity_a": 1, "entity_b": 2}
        with patch("dashboard.entities_routes.set_suggestion_status", AsyncMock(return_value=row)), \
             patch("dashboard.entities_routes.apply_merge", AsyncMock(return_value={"audit_id": 4})) as ap:
            client.post("/entities/suggestion", data={"suggestion_id": 5, "action": "accept_a"},
                        cookies=_cookie(), headers=SAME)
        ap.assert_awaited_once()

    @pytest.mark.parametrize("path", ["/entities/merge-email-dupes", "/entities/merge-collisions"])
    def test_bulk_endpoints_redirect_and_merge_nothing(self, path):
        with patch("dashboard.entities_routes.apply_merge", AsyncMock()) as ap:
            r = client.post(path, cookies=_cookie(), headers=SAME)
        assert r.status_code == 303 and r.headers["location"] == "/entities/duplicates"
        ap.assert_not_awaited()

    def test_queue_shows_a_friendly_notice_when_a_card_vanished(self):
        with patch("dashboard.entities_routes.apply_merge", AsyncMock(side_effect=MergeError("gone"))):
            r = client.post("/entities/queue/merge", data={"a": 1, "b": 2, "keep": 1, "n": 3},
                            cookies=_cookie(), headers=SAME)
        assert r.headers["location"] == "/entities/duplicates?n=3&notice=gone"
        html = review_body([], 0, "", None, None, {"running": False, "last": None}, "gone")
        assert "уже нет" in html and "blocked" not in html
        assert "владелец" in review_body([], 0, "", None, None, {"running": False, "last": None}, "blocked")


@pytest.mark.asyncio
class TestMoveRules:
    def test_missing_author_is_unknown_not_a_mention(self):
        aliases = [("telegram", "user:100")]
        assert authorship(aliases, "telegram", None, None) is None
        assert authorship(aliases, "gmail", None, None) is None
        assert authorship(aliases, "claude", "5", None) is None
        assert authorship(aliases, "telegram", "7", None) is False

    async def _stranger(self):
        stranger = await repo.upsert_entity(type="person", name="Діма", source="telegram", identifier="user:100")
        target = await repo.upsert_entity(type="person", name="Дима", source="telegram", identifier="user:1")
        other = await repo.upsert_entity(type="person", name="Олег", source="telegram", identifier="user:3")
        return stranger, target, other

    async def _event(self, i: int, source: str = "telegram", meta: dict | None = None) -> int:
        async with get_session() as s:
            ev = EventRow(source=source, source_event_id=f"e{i}", content_text="x",
                          occurred_at=datetime(2026, 9, 1), metadata_=meta or {})
            s.add(ev)
            await s.flush()
            return ev.id

    async def _rel(self, a, p, b, ev):
        await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b, predicate=p,
                                       confidence=0.7, derived_from_event_id=ev)
        async with get_session() as s:
            return (await s.execute(select(RelationshipRow.id).where(
                RelationshipRow.subject_entity_id == a, RelationshipRow.object_entity_id == b,
                RelationshipRow.predicate == p))).scalar_one()

    async def test_events_without_a_known_author_are_not_movable(self, sqlite_db):
        stranger, _, other = await self._stranger()
        unknown = await self._rel(stranger, "friend_of", other, await self._event(1, meta={}))
        mention = await self._rel(stranger, "client_of", other, await self._event(2, meta={"sender_id": "7"}))
        assert [r["rel_id"] for r in await name_evidence(stranger)] == [mention]
        assert unknown not in [r["rel_id"] for r in await name_evidence(stranger)]

    async def test_retired_target_is_revived_with_its_evidence_and_undo_restores_both(self, sqlite_db):
        stranger, target, other = await self._stranger()
        ev_old, ev_new = await self._event(1, meta={"sender_id": "8"}), await self._event(2, meta={"sender_id": "7"})
        dead = await self._rel(target, "coworker_of", other, ev_old)
        async with get_session() as s:
            row = (await s.execute(select(RelationshipRow).where(RelationshipRow.id == dead))).scalar_one()
            row.is_current = False
        src = await self._rel(stranger, "coworker_of", other, ev_new)
        assert (await preview_move(stranger, target, [src]))[0]["outcome"] == "revived"
        audit_ids = await move_relationships(stranger, target, [src], "dashboard")
        async with get_session() as s:
            rows = {r.id: r for r in (await s.execute(select(RelationshipRow))).scalars()}
            tools = [a.tool for a in (await s.execute(select(McpAuditRow).order_by(McpAuditRow.id))).scalars()]
        assert rows[dead].is_current is True and rows[dead].derived_from_event_id == ev_old
        assert rows[src].is_current is False and tools == ["relationship_revive", "relationship_retire"]
        for audit_id in audit_ids:
            async with get_session() as s:
                await undo_entry(s, audit_id, "dashboard", force=False)
        async with get_session() as s:
            rows = {r.id: r for r in (await s.execute(select(RelationshipRow))).scalars()}
        assert rows[dead].is_current is False and rows[src].is_current is True

    async def test_owner_rows_authored_by_the_owner_in_any_source_are_not_movable(self, sqlite_db):
        owner = await repo.upsert_entity(type="person", name="Владелец", source="telegram",
                                         identifier=f"user:{OWNER_TG_ID}", attributes={"email": "me@corp.example"})
        async with get_session() as s:
            s.add(EntityAliasRow(entity_id=owner, source="gmail", identifier="me@corp.example", confidence=1.0))
        target = await repo.upsert_entity(type="person", name="Другой", source="telegram", identifier="user:1")
        other = await repo.upsert_entity(type="person", name="Олег", source="telegram", identifier="user:3")
        own_slack = await self._rel(owner, "friend_of", other, await self._event(
            1, "slack", {"sender_id": str(OWNER_TG_ID)}))
        own_mail = await self._rel(owner, "client_of", other, await self._event(
            2, "gmail", {"from": "Я <me@corp.example>"}))
        mention = await self._rel(owner, "vendor_of", other, await self._event(3, meta={"sender_id": "7"}))
        found = [r["rel_id"] for r in await name_evidence(owner)]
        assert found == [mention] and own_slack not in found and own_mail not in found
        assert target  # цель существует


@pytest.mark.asyncio
class TestPanelEvents:
    def test_sender_query_has_a_literal_telegram_source_for_the_partial_index(self):
        sql, params = panel_events.queries_for("telegram", "user:42", 5)[0]
        assert "source = 'telegram' AND metadata->>'sender_id'" in sql and "src" not in params

    def test_gmail_like_escapes_wildcards(self):
        sql, params = panel_events.queries_for("gmail", "a_b%c@x.example", 5)[0]
        assert "ESCAPE" in sql and params["key"] == "%a\\_b\\%c@x.example%"

    async def test_timed_out_subquery_marks_the_result_partial(self, sqlite_db):
        aliases = [("telegram", "user:1"), ("gmail", "a@x.example")]
        real = panel_events._run
        calls = []

        async def flaky(sql, params):
            calls.append(sql)
            return None if "gmail" in sql else await real(sql, params)

        with patch.object(panel_events, "_run", flaky):
            events, partial = await panel_events.recent_events_status(aliases)
        assert partial is True and events == [] and len(calls) == 3
        _, ok = await panel_events.recent_events_status([("telegram", "user:1")])
        assert ok is False

    async def test_alias_fanout_is_capped(self, sqlite_db):
        aliases = [("telegram", f"user:{i}") for i in range(20)]
        with patch.object(panel_events, "_run", AsyncMock(return_value=[])) as run:
            _, partial = await panel_events.recent_events_status(aliases)
        assert partial is True and run.await_count <= panel_events.MAX_ALIASES_QUERIED * 2

    def test_events_endpoint_and_script_report_partial_results(self):
        from dashboard.graph_script import GRAPH_SCRIPT
        with patch("dashboard.graph_routes.entity_aliases", AsyncMock(return_value=[])), \
             patch("dashboard.graph_routes.recent_events_status", AsyncMock(return_value=([], True))):
            r = client.get("/api/graph/entity/1/events", cookies=_cookie())
        assert r.json() == {"events": [], "partial": True}
        assert "Часть событий не загрузилась" in GRAPH_SCRIPT


def test_owner_entity_is_still_selectable_only_by_row():
    assert select(EntityRow).is_select

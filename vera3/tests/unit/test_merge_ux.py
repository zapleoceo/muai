"""Объединение из карточки: предпросмотр не пишет, слияние идёт через журнал и откатывается,
перенос связей трогает только строки с доказательством «упоминание имени»; эндпоинты."""
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
from vera_shared.graph import repo  # noqa: E402
from vera_shared.graph.edit import GraphEditError  # noqa: E402
from vera_shared.graph.merge_actions import apply_merge, preview_merge  # noqa: E402
from vera_shared.graph.merge_candidates import (  # noqa: E402
    entity_summaries,
    recommend_keep,
)
from vera_shared.graph.merge_guard import MergeBlocked  # noqa: E402
from vera_shared.graph.relationship_move import (  # noqa: E402
    authored_by,
    move_relationships,
    name_evidence,
    preview_move,
)
from vera_shared.journal.undo import undo_entry  # noqa: E402
from vera_shared.projects.rules import OWNER_TG_ID  # noqa: E402

client = TestClient(app)
SAME = {"Sec-Fetch-Site": "same-origin"}


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


async def person(name: str, ident: str, **attrs) -> int:
    return await repo.upsert_entity(type="person", name=name, source="telegram",
                                    identifier=f"user:{ident}", attributes=attrs or None)


async def entity_ids() -> set[int]:
    async with get_session() as s:
        return set((await s.execute(select(EntityRow.id))).scalars())


async def event(i: int, sender: str) -> int:
    async with get_session() as s:
        ev = EventRow(source="telegram", source_event_id=f"m{i}", content_text="x",
                      occurred_at=datetime(2026, 9, 1), metadata_={"sender_id": sender})
        s.add(ev)
        await s.flush()
        return ev.id


async def rel(subject: int, predicate: str, obj: int, event_id: int | None) -> int:
    await repo.upsert_relationship(subject_entity_id=subject, object_entity_id=obj, predicate=predicate,
                                   confidence=0.8, derived_from_event_id=event_id)
    async with get_session() as s:
        return (await s.execute(select(RelationshipRow.id).where(
            RelationshipRow.subject_entity_id == subject, RelationshipRow.object_entity_id == obj,
            RelationshipRow.predicate == predicate))).scalar_one()


@pytest.mark.asyncio
class TestMerge:
    async def test_preview_counts_what_moves_and_writes_nothing(self, sqlite_db):
        keep, dup, other = await person("Анна", "1"), await person("Anna", "2"), await person("Олег", "3")
        await rel(dup, "friend_of", other, None)
        plan = await preview_merge(keep, [dup], "test")
        assert plan["dry_run"] is True and plan["counts"]["relationships_moved"] == 1
        assert await entity_ids() == {keep, dup, other}
        async with get_session() as s:
            assert (await s.execute(select(McpAuditRow))).first() is None

    async def test_apply_is_journaled_as_dashboard_and_undo_restores_the_card(self, sqlite_db):
        keep, dup = await person("Анна", "1"), await person("Anna", "2")
        result = await apply_merge(keep, [dup], "test", "dashboard")
        assert await entity_ids() == {keep}
        async with get_session() as s:
            entry = (await s.execute(select(McpAuditRow))).scalar_one()
        assert (entry.client, entry.tool, entry.target_kind, entry.id) == (
            "dashboard", "entity_merge", "merge", result["audit_id"])
        async with get_session() as s:
            await undo_entry(s, result["audit_id"], "dashboard", force=False)
        assert await entity_ids() == {keep, dup}

    async def test_owner_cannot_be_merged_without_force(self, sqlite_db):
        owner, dup = await person("Владелец", str(OWNER_TG_ID)), await person("Владелец 2", "9")
        plan = await preview_merge(owner, [dup], "test")
        assert plan["would_be_refused"] is True and plan["counts"] is None
        with pytest.raises(MergeBlocked):
            await apply_merge(owner, [dup], "test", "dashboard")
        assert await entity_ids() == {owner, dup}

    async def test_the_card_with_more_data_is_recommended_to_stay(self, sqlite_db):
        rich, poor, other = await person("Богатая", "1"), await person("Бедная", "2"), await person("О", "3")
        await rel(rich, "friend_of", other, None)
        data = await entity_summaries([rich, poor])
        assert recommend_keep(data[rich], data[poor]) == rich
        assert recommend_keep(data[poor], data[rich]) == rich


@pytest.mark.asyncio
class TestMoveRelationships:
    async def _world(self):
        stranger = await person("Діма", "100", username="low1st777")
        owner_dima, other, third = await person("Дима Владелец", "1"), await person("Олег", "3"), await person("Ира", "4")
        own_ev, mention_ev = await event(1, "100"), await event(2, "7")
        own = await rel(stranger, "friend_of", other, own_ev)               # написал сам аккаунт
        mention = await rel(stranger, "coworker_of", third, mention_ev)     # чужая реплика с именем
        manual = await rel(stranger, "spouse_of", other, None)              # заведено вручную
        return stranger, owner_dima, third, own, mention, manual

    def test_authorship_rule(self):
        aliases = [("telegram", "user:100"), ("gmail", "Dima@x.example")]
        assert authored_by(aliases, "telegram", "100", None) is True
        assert authored_by(aliases, "telegram", "7", None) is False
        assert authored_by(aliases, "gmail", None, "Dima <dima@x.example>") is True
        assert authored_by(aliases, "gmail", None, "Other <o@x.example>") is False

    async def test_only_name_mention_rows_are_candidates(self, sqlite_db):
        stranger, _, _, own, mention, manual = await self._world()
        found = await name_evidence(stranger)
        assert [r["rel_id"] for r in found] == [mention] and own not in [r["rel_id"] for r in found]
        assert manual not in [r["rel_id"] for r in found]

    async def test_preview_writes_nothing_then_move_repoints_and_journals(self, sqlite_db):
        stranger, right, third, own, mention, manual = await self._world()
        assert await preview_move(stranger, right, [mention]) == [{"rel_id": mention, "outcome": "moved"}]
        async with get_session() as s:
            row = (await s.execute(select(RelationshipRow).where(RelationshipRow.id == mention))).scalar_one()
            assert row.subject_entity_id == stranger or row.object_entity_id == stranger
            assert (await s.execute(select(McpAuditRow))).first() is None
        audit_ids = await move_relationships(stranger, right, [mention], "dashboard")
        async with get_session() as s:
            rows = {r.id: r for r in (await s.execute(select(RelationshipRow))).scalars()}
            entry = (await s.execute(select(McpAuditRow))).scalar_one()
        assert stranger not in (rows[mention].subject_entity_id, rows[mention].object_entity_id)
        assert right in (rows[mention].subject_entity_id, rows[mention].object_entity_id)
        assert stranger in (rows[own].subject_entity_id, rows[own].object_entity_id)        # свои не тронуты
        assert stranger in (rows[manual].subject_entity_id, rows[manual].object_entity_id)
        assert (entry.client, entry.tool) == ("dashboard", "relationship_move") and entry.id == audit_ids[0]
        async with get_session() as s:
            await undo_entry(s, audit_ids[0], "dashboard", force=False)
            back = (await s.execute(select(RelationshipRow).where(RelationshipRow.id == mention))).scalar_one()
        assert stranger in (back.subject_entity_id, back.object_entity_id)

    async def test_accounts_are_not_merged_by_a_move(self, sqlite_db):
        stranger, right, _, _, mention, _ = await self._world()
        await move_relationships(stranger, right, [mention], "dashboard")
        assert {stranger, right} <= await entity_ids()
        async with get_session() as s:
            aliases = (await s.execute(select(EntityAliasRow.entity_id, EntityAliasRow.identifier))).all()
        assert (stranger, "user:100") in aliases

    async def test_duplicate_triple_is_retired_instead_of_moved(self, sqlite_db):
        stranger, right, third, _, mention, _ = await self._world()
        await rel(right, "coworker_of", third, None)
        assert (await preview_move(stranger, right, [mention]))[0]["outcome"] == "retired"
        (audit_id,) = await move_relationships(stranger, right, [mention], "dashboard")
        async with get_session() as s:
            entry = (await s.execute(select(McpAuditRow).where(McpAuditRow.id == audit_id))).scalar_one()
        assert entry.tool == "relationship_retire"

    async def test_refuses_rows_that_are_not_name_evidence(self, sqlite_db):
        stranger, right, _, own, _, manual = await self._world()
        for bad in ([own], [manual]):
            with pytest.raises(GraphEditError, match="not name-mention evidence"):
                await move_relationships(stranger, right, bad, "dashboard")
        with pytest.raises(GraphEditError, match="differ"):
            await move_relationships(stranger, stranger, [own], "dashboard")


class TestEndpoints:
    PATHS = {
        "/api/graph/merge/preview": {"a": 1, "b": 2},
        "/api/graph/merge/apply": {"keep_id": 1, "drop_id": 2},
        "/api/graph/move/preview": {"from_id": 1, "to_id": 2},
        "/api/graph/move/apply": {"from_id": 1, "to_id": 2, "rel_ids": [5]},
    }

    @pytest.mark.parametrize("path", list(PATHS))
    def test_owner_only_and_same_origin(self, path):
        assert client.post(path, json=self.PATHS[path], headers=SAME).status_code == 401
        r = client.post(path, json=self.PATHS[path], cookies=_cookie(),
                        headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_search_needs_the_owner_and_a_real_query(self):
        assert client.get("/api/graph/people/search?q=ан").status_code == 401
        r = client.get("/api/graph/people/search?q=а", cookies=_cookie())
        assert r.json() == {"people": []}

    def test_search_excludes_the_open_card_and_returns_summaries(self):
        found = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        with patch("dashboard.merge_routes.search_entities", AsyncMock(return_value=found)), \
             patch("dashboard.merge_routes.entity_summaries",
                   AsyncMock(return_value={2: {"id": 2, "name": "B"}})):
            r = client.get("/api/graph/people/search?q=бо&exclude=1", cookies=_cookie())
        assert r.json() == {"people": [{"id": 2, "name": "B"}]}

    def test_merge_preview_offers_the_richer_card_and_switch(self):
        people = {1: {"id": 1, "aliases": 5, "relationships": 5, "groups": 0, "name": "A"},
                  2: {"id": 2, "aliases": 1, "relationships": 0, "groups": 0, "name": "B"}}
        plan = {"counts": {"relationships_moved": 1}, "blockers": [], "would_be_refused": False}
        with patch("dashboard.merge_routes.entity_summaries", AsyncMock(return_value=people)), \
             patch("dashboard.merge_routes.preview_merge", AsyncMock(return_value=plan)) as prev:
            r = client.post("/api/graph/merge/preview", json={"a": 2, "b": 1}, cookies=_cookie(), headers=SAME)
            assert r.json()["keep"]["id"] == 1 and r.json()["recommended_keep"] == 1
            assert prev.await_args.args[:2] == (1, [2])
            r = client.post("/api/graph/merge/preview", json={"a": 2, "b": 1, "keep_id": 2},
                            cookies=_cookie(), headers=SAME)
            assert r.json()["keep"]["id"] == 2

    def test_merge_apply_uses_the_journaled_path_and_reports_blocks(self):
        with patch("dashboard.merge_routes.apply_merge",
                   AsyncMock(return_value={"audit_id": 8, "counts": {}})) as apply:
            r = client.post("/api/graph/merge/apply", json={"keep_id": 1, "drop_id": 2},
                            cookies=_cookie(), headers=SAME)
        assert r.json()["audit_ids"] == [8] and apply.await_args.args[3] == "dashboard"
        with patch("dashboard.merge_routes.apply_merge", AsyncMock(side_effect=MergeBlocked("owner"))):
            r = client.post("/api/graph/merge/apply", json={"keep_id": 1, "drop_id": 2},
                            cookies=_cookie(), headers=SAME)
        assert r.status_code == 409

    def test_move_apply_passes_selected_rows(self):
        with patch("dashboard.merge_routes.move_relationships", AsyncMock(return_value=[3])) as move:
            r = client.post("/api/graph/move/apply", json={"from_id": 1, "to_id": 2, "rel_ids": [5, 6]},
                            cookies=_cookie(), headers=SAME)
        assert r.json() == {"ok": True, "audit_ids": [3]}
        move.assert_awaited_once_with(1, 2, [5, 6], "dashboard")

    def test_card_script_has_both_flows_and_the_difference_copy(self):
        from dashboard.graph_script import GRAPH_SCRIPT
        for needle in ("Это тот же человек", "Связи не про этого человека", "/api/graph/merge/preview",
                       "/api/graph/move/apply", "Объединить — это один человек с двумя аккаунтами",
                       "Поменять местами", "openMergeDialog"):
            assert needle in GRAPH_SCRIPT

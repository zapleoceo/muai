"""Слияние сущностей одной транзакцией и откат по отчёту.

Главный инвариант: merge → unmerge возвращает граф байт-в-байт. Проверяется
сравнением полного дампа всех графовых таблиц до слияния и после отката.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import (
    EntityAliasRow,
    EntityAvatarRow,
    EntityRow,
    IdentityNodeRow,
    MembershipRow,
    MergeSuggestionRow,
    RelationshipRow,
)
from vera_shared.graph import entity_edit
from vera_shared.graph.merge import MergeError, merge_entities, union_attributes
from vera_shared.graph.merge_children import split_alias_conflicts
from vera_shared.graph.merge_codec import row_dict
from vera_shared.graph.merge_report import MergeReport
from vera_shared.graph.unmerge import UnmergeError, unmerge

_T1 = datetime(2026, 1, 1)
_T2 = datetime(2026, 6, 1)
_T3 = datetime(2026, 9, 1)

_TABLES = (EntityRow, EntityAliasRow, MembershipRow, RelationshipRow,
           EntityAvatarRow, IdentityNodeRow, MergeSuggestionRow)


async def _dump() -> dict:
    out = {}
    async with get_session() as s:
        for cls in _TABLES:
            rows = (await s.execute(select(cls))).scalars().all()
            out[cls.__tablename__] = sorted(
                (row_dict(r) for r in rows), key=lambda d: json.dumps(d, sort_keys=True))
    return out


async def _world() -> dict[str, int]:
    """keep K и два дубля D1, D2; соседи X (чат) и Y."""
    ids: dict[str, int] = {}
    async with get_session() as s:
        for key, type_, name, attrs, first, last in (
            ("K", "person", "Keep", {"tg_id": 1, "title": "boss"}, _T2, _T3),
            ("D1", "person", "Dup One", {"title": "other", "phone": "+1"}, _T1, _T2),
            ("D2", "person", "Dup Two", {"slack": "U1"}, _T1, _T3),
            ("X", "supergroup", "Chat", {}, _T1, _T3),
            ("Y", "organization", "Org", {}, _T1, _T3),
        ):
            row = EntityRow(type=type_, name=name, attributes=attrs,
                            first_seen_at=first, last_seen_at=last)
            s.add(row)
            await s.flush()
            ids[key] = row.id
        k, d1, d2, x, y = (ids[n] for n in ("K", "D1", "D2", "X", "Y"))
        s.add_all([
            EntityAliasRow(entity_id=k, source="telegram", identifier="user:1"),
            EntityAliasRow(entity_id=d1, source="gmail", identifier="a@x.org"),
            EntityAliasRow(entity_id=d2, source="slack", identifier="user:U1"),
            MembershipRow(parent_entity_id=x, child_entity_id=k, source="telegram",
                          first_seen_at=_T2, last_seen_at=_T3, is_current=False),
            MembershipRow(parent_entity_id=x, child_entity_id=d1, source="telegram",
                          first_seen_at=_T1, last_seen_at=_T2, is_current=True),
            MembershipRow(parent_entity_id=d2, child_entity_id=y, source="slack"),
            MembershipRow(parent_entity_id=x, child_entity_id=d2, source="slack"),
            RelationshipRow(subject_entity_id=k, object_entity_id=x, predicate="IN",
                            confidence=0.5, is_current=False, fact=None,
                            first_seen_at=_T2, last_seen_at=_T3),
            RelationshipRow(subject_entity_id=d1, object_entity_id=x, predicate="IN",
                            confidence=0.9, is_current=True, fact="was there",
                            first_seen_at=_T1, last_seen_at=_T2),
            RelationshipRow(subject_entity_id=d1, object_entity_id=y, predicate="WORKS_AT"),
            RelationshipRow(subject_entity_id=y, object_entity_id=d2, predicate="HIRED"),
            RelationshipRow(subject_entity_id=k, object_entity_id=d1, predicate="SAME_AS"),
            EntityAvatarRow(entity_id=d1, image=b"\x89PNG\x00\x01", mime="image/png"),
            IdentityNodeRow(type="style", label="tone", listener_entity_id=d2),
            MergeSuggestionRow(entity_a=d1, entity_b=y, verdict="unsure"),
            MergeSuggestionRow(entity_a=k, entity_b=d1, verdict="same"),
        ])
    return ids


@pytest.mark.usefixtures("sqlite_db")
class TestMerge:

    @pytest.mark.asyncio
    async def test_everything_lands_on_keep(self):
        ids = await _world()
        await merge_entities(ids["K"], [ids["D1"], ids["D2"]], "test")
        k = ids["K"]
        async with get_session() as s:
            assert {e.id for e in (await s.execute(select(EntityRow))).scalars()} == {
                k, ids["X"], ids["Y"]}
            aliases = (await s.execute(select(EntityAliasRow))).scalars().all()
            assert {a.entity_id for a in aliases} == {k}
            assert {(a.source, a.identifier) for a in aliases} == {
                ("telegram", "user:1"), ("gmail", "a@x.org"), ("slack", "user:U1")}
            node = (await s.execute(select(IdentityNodeRow))).scalar_one()
            assert node.listener_entity_id == k
            avatar = (await s.execute(select(EntityAvatarRow))).scalar_one()
            assert avatar.entity_id == k
            assert avatar.image == b"\x89PNG\x00\x01"

    @pytest.mark.asyncio
    async def test_relationships_deduped_keeping_max_confidence_and_current(self):
        ids = await _world()
        await merge_entities(ids["K"], [ids["D1"], ids["D2"]], "test")
        async with get_session() as s:
            rels = (await s.execute(select(RelationshipRow))).scalars().all()
        by_pred = {r.predicate: r for r in rels}
        assert len(rels) == 3  # IN (склеено), WORKS_AT, HIRED; SAME_AS стал петлёй и удалён
        assert "SAME_AS" not in by_pred
        edge = by_pred["IN"]
        assert edge.confidence == 0.9
        assert edge.is_current is True
        assert edge.fact == "was there"
        assert edge.first_seen_at == _T1 and edge.last_seen_at == _T3
        assert by_pred["WORKS_AT"].subject_entity_id == ids["K"]
        assert by_pred["HIRED"].object_entity_id == ids["K"]

    @pytest.mark.asyncio
    async def test_memberships_deduped_and_self_membership_dropped(self):
        ids = await _world()
        await merge_entities(ids["K"], [ids["D1"], ids["D2"]], "test")
        async with get_session() as s:
            mems = (await s.execute(select(MembershipRow))).scalars().all()
        keys = {(m.parent_entity_id, m.child_entity_id, m.source) for m in mems}
        assert keys == {(ids["X"], ids["K"], "telegram"),
                        (ids["K"], ids["Y"], "slack"),
                        (ids["X"], ids["K"], "slack")}
        tg = next(m for m in mems if m.source == "telegram")
        assert tg.is_current is True and tg.first_seen_at == _T1

    @pytest.mark.asyncio
    async def test_suggestions_follow_keep_and_self_pair_is_dropped(self):
        ids = await _world()
        await merge_entities(ids["K"], [ids["D1"], ids["D2"]], "test")
        async with get_session() as s:
            sug = (await s.execute(select(MergeSuggestionRow))).scalars().all()
        assert len(sug) == 1
        assert {sug[0].entity_a, sug[0].entity_b} == {ids["K"], ids["Y"]}
        assert sug[0].entity_a < sug[0].entity_b

    @pytest.mark.asyncio
    async def test_attributes_union_keeps_win_and_dropped_values_recorded(self):
        ids = await _world()
        await merge_entities(ids["K"], [ids["D1"]], "test")
        async with get_session() as s:
            keep = await s.get(EntityRow, ids["K"])
        assert keep.attributes["title"] == "boss"
        assert keep.attributes["phone"] == "+1"
        note = keep.attributes["merged_from"][0]
        assert note["name"] == "Dup One" and note["differing"] == {"title": "other"}
        assert keep.first_seen_at == _T1 and keep.last_seen_at == _T3

    @pytest.mark.asyncio
    async def test_avatar_prefers_real_photo_over_missing_marker(self):
        ids = await _world()
        async with get_session() as s:
            s.add(EntityAvatarRow(entity_id=ids["K"], image=None, missing=True))
        await merge_entities(ids["K"], [ids["D1"]], "test")
        async with get_session() as s:
            av = (await s.execute(select(EntityAvatarRow))).scalar_one()
        assert av.entity_id == ids["K"] and av.image == b"\x89PNG\x00\x01"

    @pytest.mark.asyncio
    async def test_refuses_bad_input(self):
        ids = await _world()
        with pytest.raises(MergeError):
            await merge_entities(ids["K"], [], "x")
        with pytest.raises(MergeError):
            await merge_entities(ids["K"], [ids["K"]], "x")
        with pytest.raises(MergeError):
            await merge_entities(ids["K"], [999999], "x")


@pytest.mark.usefixtures("sqlite_db")
class TestUnmerge:

    @pytest.mark.asyncio
    async def test_round_trip_restores_exact_state_through_json(self):
        ids = await _world()
        before = await _dump()
        report = await merge_entities(ids["K"], [ids["D1"], ids["D2"]], "test")
        assert await _dump() != before
        wire = json.loads(json.dumps(report.to_dict()))
        await unmerge(wire)
        assert await _dump() == before

    @pytest.mark.asyncio
    async def test_single_drop_round_trip(self):
        ids = await _world()
        before = await _dump()
        report = await merge_entities(ids["K"], [ids["D2"]], "test")
        await unmerge(report)
        assert await _dump() == before

    @pytest.mark.asyncio
    async def test_suggestion_pair_that_swaps_order_restores_without_collision(self):
        ids = await _world()
        before = await _dump()
        report = await merge_entities(ids["Y"], [ids["K"]], "test")  # keep с бо́льшим id
        await unmerge(report)
        assert await _dump() == before

    @pytest.mark.asyncio
    async def test_unmerge_nulls_link_to_pruned_event(self):
        from vera_shared.db.models import EventRow
        from vera_shared.timeutil import utc_naive_now
        ids = await _world()
        now = utc_naive_now()
        async with get_session() as s:
            ev = EventRow(source="telegram", source_event_id="e", content_text="x",
                          occurred_at=now, received_at=now, triage_status="done")
            s.add(ev)
            await s.flush()
            drop_edge = (await s.execute(select(RelationshipRow).where(
                RelationshipRow.subject_entity_id == ids["D1"],
                RelationshipRow.predicate == "IN"))).scalar_one()
            drop_edge.derived_from_event_id = ev.id
        report = await merge_entities(ids["K"], [ids["D1"], ids["D2"]], "test")
        async with get_session() as s:
            await s.delete(await s.get(EventRow, ev.id))
        await unmerge(report)
        async with get_session() as s:
            edge = (await s.execute(select(RelationshipRow).where(
                RelationshipRow.subject_entity_id == ids["D1"],
                RelationshipRow.predicate == "IN"))).scalar_one()
        assert edge.derived_from_event_id is None

    @pytest.mark.asyncio
    async def test_unmerge_refuses_when_id_is_taken(self):
        ids = await _world()
        report = await merge_entities(ids["K"], [ids["D1"]], "test")
        await unmerge(report)
        with pytest.raises(UnmergeError):
            await unmerge(report)

    @pytest.mark.asyncio
    async def test_report_counts_name_the_tables(self):
        ids = await _world()
        report = await merge_entities(ids["K"], [ids["D1"], ids["D2"]], "test")
        counts = report.counts()
        assert counts["entity_aliases_moved"] == 2
        assert counts["relationships_deleted"] >= 2
        assert MergeReport.from_dict(report.to_dict()) == report


class TestPureHelpers:

    def test_alias_conflict_is_split_off(self):
        keep = {("gmail", "a@x.org")}
        same = EntityAliasRow(entity_id=2, source="gmail", identifier="a@x.org")
        other = EntityAliasRow(entity_id=2, source="slack", identifier="u1")
        moves, conflicts = split_alias_conflicts(keep, [same, other])
        assert moves == [other] and conflicts == [same]

    def test_union_attributes_is_pure_and_keeps_history(self):
        drop = EntityRow(id=7, type="person", name="D", attributes={"a": 2, "b": 3})
        out = union_attributes({"a": 1, "merged_from": [{"id": 1}]}, [drop])
        assert out["a"] == 1 and out["b"] == 3
        assert [m["id"] for m in out["merged_from"]] == [1, 7]


@pytest.mark.usefixtures("sqlite_db")
class TestEntityEdit:

    @pytest.mark.asyncio
    async def test_retype_and_revert(self):
        ids = await _world()
        report = await entity_edit.retype_entity(ids["K"], "organization")
        assert report["old"] == "person" and report["new"] == "organization"
        await entity_edit.revert_edit(report)
        async with get_session() as s:
            assert (await s.get(EntityRow, ids["K"])).type == "person"

    @pytest.mark.asyncio
    async def test_rename_rejects_blank_and_retype_rejects_unknown(self):
        ids = await _world()
        with pytest.raises(MergeError):
            await entity_edit.rename_entity(ids["K"], "  ")
        with pytest.raises(MergeError):
            await entity_edit.retype_entity(ids["K"], "spaceship")

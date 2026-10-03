"""MCP write-инструменты: каждая правка пишет аудит, `undo` её откатывает."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select
from vera_mcp import write_tools as w
from vera_mcp.audit import AuditNotFound
from vera_mcp.undo import UndoRefused
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import EntityAliasRow, EntityRow, RelationshipRow
from vera_shared.db.models_mcp import McpAuditRow
from vera_shared.events.edit import EventBusy, EventNotFound
from vera_shared.graph.edit import GraphEditError
from vera_shared.graph.merge_errors import MergeError
from vera_shared.memory.remember import RememberOutcome

pytestmark = pytest.mark.asyncio


def ctx(client: str = "claude"):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


async def add_event(event_id: int = 1, text: str = "original", status: str = "done",
                    **kw) -> int:
    async with get_session() as s:
        s.add(EventRow(id=event_id, source="gmail", source_event_id=f"e{event_id}",
                       content_text=text, occurred_at=datetime(2026, 1, 1),
                       triage_status=status, **kw))
    return event_id


async def add_entity(name: str, entity_id: int | None = None) -> int:
    async with get_session() as s:
        e = EntityRow(id=entity_id, type="person", name=name, attributes={})
        s.add(e)
        await s.flush()
        return e.id


async def event(event_id: int = 1) -> EventRow:
    async with get_session() as s:
        return (await s.execute(select(EventRow).where(EventRow.id == event_id))).scalar_one()


async def audit_rows() -> list[McpAuditRow]:
    async with get_session() as s:
        return list((await s.execute(select(McpAuditRow).order_by(McpAuditRow.id))).scalars())


# ─── remember ────────────────────────────────────────────────────────────────


def fake_remember(outcome: RememberOutcome):
    """Как настоящая remember_fact: журнал зовётся из её транзакции, но не при точном дубле."""
    async def _fake(text, kind, context, tags, *, on_written=None):
        if outcome.dedup_reason != "exact" and on_written is not None:
            async with get_session() as s:
                await on_written(s, outcome)
        return outcome
    return _fake


async def test_remember_audits_new_event_and_undo_hides_it(sqlite_db):
    await add_event(5, "a fact worth keeping", status="pending")
    outcome = RememberOutcome(event_id=5, deduped=False)
    with patch("vera_mcp.write_tools.remember_fact", fake_remember(outcome)):
        res = await w.remember("a fact worth keeping", ctx("codex"), tags=["x"])
    assert res["event_id"] == 5 and res["audit_id"]
    (row,) = await audit_rows()
    assert (row.client, row.tool, row.target_kind, row.target_id) == (
        "codex", "remember", "event", 5)
    assert row.args["tags"] == ["x"]

    await w.undo(res["audit_id"], ctx())
    assert (await event(5)).triage_status == "hidden"
    assert (await event(5)).content_text == "a fact worth keeping"   # ничего не удалено


async def test_remember_undo_refused_after_the_event_was_edited(sqlite_db):
    await add_event(5, "a fact worth keeping", status="done")
    with patch("vera_mcp.write_tools.remember_fact",
               fake_remember(RememberOutcome(event_id=5, deduped=False))):
        res = await w.remember("a fact worth keeping", ctx())
    await w.update_event(5, ctx(), content_text="edited later")
    with pytest.raises(UndoRefused, match="force"):
        await w.undo(res["audit_id"], ctx())
    assert (await event(5)).triage_status == "pending"
    await w.undo(res["audit_id"], ctx(), force=True)
    assert (await event(5)).triage_status == "hidden"


async def test_remember_exact_duplicate_writes_no_audit(sqlite_db):
    outcome = RememberOutcome(event_id=5, deduped=True, dedup_reason="exact")
    with patch("vera_mcp.write_tools.remember_fact", fake_remember(outcome)):
        res = await w.remember("already known", ctx())
    assert res["deduped"] is True and res["audit_id"] is None
    assert await audit_rows() == []


async def test_remember_semantic_duplicate_is_audited(sqlite_db):
    await add_event(9, "dup", status="superseded")
    outcome = RememberOutcome(event_id=9, deduped=True, dedup_reason="semantic",
                              similar_event_id=2, similarity=0.95)
    with patch("vera_mcp.write_tools.remember_fact", fake_remember(outcome)):
        res = await w.remember("almost the same", ctx())
    assert res["audit_id"] and res["similar_event_id"] == 2


# ─── update_event ────────────────────────────────────────────────────────────


async def test_update_event_keeps_previous_version_and_requeues(sqlite_db):
    await add_event(1, "old text", metadata_={"a": 1, "b": 2}, category="generic")
    res = await w.update_event(1, ctx(), content_text="new text",
                               metadata={"a": None, "c": 3}, category="decision")
    assert res["requeued_for_embedding"] is True
    ev = await event()
    assert (ev.content_text, ev.metadata_, ev.category) == (
        "new text", {"b": 2, "c": 3}, "decision")
    assert ev.triage_status == "pending"
    (row,) = await audit_rows()
    assert row.before["content_text"] == "old text"
    assert row.before["metadata"] == {"a": 1, "b": 2}
    assert row.after["content_text"] == "new text"


async def test_update_event_metadata_only_does_not_requeue(sqlite_db):
    await add_event(1, "same")
    res = await w.update_event(1, ctx(), metadata={"k": "v"})
    assert res["requeued_for_embedding"] is False
    assert (await event()).triage_status == "done"


async def test_update_event_on_hidden_event_stays_hidden(sqlite_db):
    await add_event(1, "old")
    await w.hide_event(1, ctx())
    await w.update_event(1, ctx(), content_text="new")
    assert (await event()).triage_status == "hidden"


async def test_update_event_undo_restores_text_and_reembeds(sqlite_db):
    await add_event(1, "old text")
    res = await w.update_event(1, ctx(), content_text="new text")
    await w.undo(res["audit_id"], ctx())
    ev = await event()
    assert ev.content_text == "old text"
    assert ev.triage_status == "pending"                    # пере-эмбеддинг старого текста
    rows = await audit_rows()
    assert [r.status for r in rows] == ["undone", "applied"]
    assert rows[1].tool == "undo" and rows[1].undo_of == rows[0].id


async def test_update_event_undo_refuses_when_changed_later(sqlite_db):
    await add_event(1, "v0")
    first = await w.update_event(1, ctx(), content_text="v1")
    await w.update_event(1, ctx(), content_text="v2")
    with pytest.raises(UndoRefused, match="force"):
        await w.undo(first["audit_id"], ctx())
    assert (await event()).content_text == "v2"
    await w.undo(first["audit_id"], ctx(), force=True)
    assert (await event()).content_text == "v0"


async def test_update_event_validation(sqlite_db):
    with pytest.raises(ValueError, match="nothing to change"):
        await w.update_event(1, ctx())
    with pytest.raises(EventNotFound):
        await w.update_event(404, ctx(), content_text="x")
    assert await audit_rows() == []


# ─── hide / unhide ───────────────────────────────────────────────────────────


async def test_hide_and_unhide_restore_the_exact_previous_status(sqlite_db):
    await add_event(1, "broken", status="error", triage_metadata={"k": 1})
    res = await w.hide_event(1, ctx())
    assert res["hidden"] is True
    assert (await event()).triage_status == "hidden"
    await w.unhide_event(1, ctx())
    ev = await event()
    assert (ev.triage_status, ev.triage_metadata) == ("error", {"k": 1})
    assert [r.tool for r in await audit_rows()] == ["hide_event", "unhide_event"]


async def test_hide_twice_keeps_original_status(sqlite_db):
    await add_event(1, "x", status="done")
    await w.hide_event(1, ctx())
    await w.hide_event(1, ctx())
    await w.unhide_event(1, ctx())
    assert (await event()).triage_status == "done"


async def test_unhide_without_hide_changes_nothing(sqlite_db):
    await add_event(1, "x", status="pending")
    await w.unhide_event(1, ctx())
    assert (await event()).triage_status == "pending"


async def test_hide_undo_restores_status(sqlite_db):
    await add_event(1, "x", status="done")
    res = await w.hide_event(1, ctx())
    await w.undo(res["audit_id"], ctx())
    assert (await event()).triage_status == "done"


async def test_hide_undo_refused_after_unhide(sqlite_db):
    await add_event(1, "x", status="done")
    hid = await w.hide_event(1, ctx())
    await w.unhide_event(1, ctx())
    with pytest.raises(UndoRefused, match="force"):
        await w.undo(hid["audit_id"], ctx())


# ─── граф ────────────────────────────────────────────────────────────────────


async def test_entity_rename_audits_and_undo_restores(sqlite_db):
    eid = await add_entity("Old Name")
    res = await w.entity_rename(eid, "New Name", ctx())
    async with get_session() as s:
        assert (await s.get(EntityRow, eid)).name == "New Name"
    (row,) = await audit_rows()
    assert (row.before, row.after) == ({"name": "Old Name"}, {"name": "New Name"})
    await w.undo(res["audit_id"], ctx())
    async with get_session() as s:
        assert (await s.get(EntityRow, eid)).name == "Old Name"


async def test_entity_rename_undo_refused_when_renamed_again(sqlite_db):
    eid = await add_entity("A")
    first = await w.entity_rename(eid, "B", ctx())
    await w.entity_rename(eid, "C", ctx())
    with pytest.raises(UndoRefused):
        await w.undo(first["audit_id"], ctx())
    await w.undo(first["audit_id"], ctx(), force=True)
    async with get_session() as s:
        assert (await s.get(EntityRow, eid)).name == "A"


async def test_entity_rename_unknown_entity(sqlite_db):
    with pytest.raises(GraphEditError, match="not found"):
        await w.entity_rename(99, "x", ctx())


async def test_add_alias_is_idempotent_and_undoable(sqlite_db):
    eid = await add_entity("Person")
    res = await w.entity_add_alias(eid, "gmail", "p@example.com", ctx(), "P")
    assert res["created"] is True and res["audit_id"]
    again = await w.entity_add_alias(eid, "gmail", "p@example.com", ctx())
    assert again["created"] is False and again["audit_id"] is None
    assert len(await audit_rows()) == 1

    await w.undo(res["audit_id"], ctx())
    async with get_session() as s:
        assert (await s.execute(select(EntityAliasRow))).scalars().all() == []


async def test_add_alias_owned_by_other_entity_is_refused(sqlite_db):
    a, b = await add_entity("A"), await add_entity("B")
    await w.entity_add_alias(a, "telegram", "user:1", ctx())
    with pytest.raises(GraphEditError, match="already belongs"):
        await w.entity_add_alias(b, "telegram", "user:1", ctx())


async def rel_row(rel_id: int) -> RelationshipRow:
    async with get_session() as s:
        return await s.get(RelationshipRow, rel_id)


async def test_relationship_set_new_then_undo_retires(sqlite_db):
    a, b = await add_entity("A"), await add_entity("B")
    res = await w.relationship_set(a, b, "works_at", ctx(), fact="since 2020", confidence=0.9)
    assert res["created"] is True
    row = await rel_row(res["relationship_id"])
    assert (row.predicate, row.fact, row.confidence, row.is_current) == (
        "works_at", "since 2020", 0.9, True)
    await w.undo(res["audit_id"], ctx())
    assert (await rel_row(res["relationship_id"])).is_current is False   # снята, не удалена


async def test_relationship_set_existing_updates_and_undo_restores(sqlite_db):
    a, b = await add_entity("A"), await add_entity("B")
    first = await w.relationship_set(a, b, "friend_of", ctx(), fact="v1", confidence=0.5)
    await w.relationship_retire(first["relationship_id"], ctx())
    second = await w.relationship_set(a, b, "friend_of", ctx(), fact="v2", confidence=0.9)
    assert second["created"] is False
    assert second["relationship_id"] == first["relationship_id"]
    row = await rel_row(first["relationship_id"])
    assert (row.fact, row.is_current) == ("v2", True)

    await w.undo(second["audit_id"], ctx())
    row = await rel_row(first["relationship_id"])
    assert (row.fact, row.confidence, row.is_current) == ("v1", 0.5, False)


async def test_relationship_retire_and_undo(sqlite_db):
    a, b = await add_entity("A"), await add_entity("B")
    made = await w.relationship_set(a, b, "boss_of", ctx())
    res = await w.relationship_retire(made["relationship_id"], ctx())
    assert (await rel_row(made["relationship_id"])).is_current is False
    await w.undo(res["audit_id"], ctx())
    assert (await rel_row(made["relationship_id"])).is_current is True


async def test_relationship_undo_refused_when_changed_later(sqlite_db):
    a, b = await add_entity("A"), await add_entity("B")
    made = await w.relationship_set(a, b, "boss_of", ctx(), fact="one")
    await w.relationship_set(a, b, "boss_of", ctx(), fact="two")
    with pytest.raises(UndoRefused):
        await w.undo(made["audit_id"], ctx())


async def test_relationship_validation(sqlite_db):
    a, b = await add_entity("A"), await add_entity("B")
    with pytest.raises(GraphEditError, match="unknown predicate"):
        await w.relationship_set(a, b, "nemesis_of", ctx())
    with pytest.raises(GraphEditError, match="must differ"):
        await w.relationship_set(a, a, "friend_of", ctx())
    with pytest.raises(GraphEditError, match="not found"):
        await w.relationship_set(a, 999, "friend_of", ctx())
    with pytest.raises(GraphEditError, match="not found"):
        await w.relationship_retire(999, ctx())
    assert await audit_rows() == []


# ─── undo: общие правила ─────────────────────────────────────────────────────


async def test_undo_twice_and_undo_of_undo_are_refused(sqlite_db):
    await add_event(1, "x")
    hid = await w.hide_event(1, ctx())
    undone = await w.undo(hid["audit_id"], ctx("codex"))
    assert undone["undone"] == hid["audit_id"]
    with pytest.raises(UndoRefused, match="already undone"):
        await w.undo(hid["audit_id"], ctx())
    with pytest.raises(UndoRefused, match="itself an undo"):
        await w.undo(undone["audit_id"], ctx())
    assert (await audit_rows())[-1].client == "codex"


async def test_undo_unknown_audit_id(sqlite_db):
    with pytest.raises(AuditNotFound):
        await w.undo(12345, ctx())


async def test_undo_unsupported_kind(sqlite_db):
    async with get_session() as s:
        s.add(McpAuditRow(client="c", tool="x", args={}, target_kind="mystery"))
    with pytest.raises(UndoRefused, match="mystery"):
        await w.undo(1, ctx())


# ─── откат трогает только свои поля ──────────────────────────────────────────


async def test_hide_undo_does_not_overwrite_a_text_edit_made_while_hidden(sqlite_db):
    await add_event(1, "v0", status="done")
    hid = await w.hide_event(1, ctx())
    await w.update_event(1, ctx(), content_text="v1", metadata={"k": "new"})
    await w.undo(hid["audit_id"], ctx())
    ev = await event()
    assert (ev.content_text, ev.metadata_) == ("v1", {"k": "new"})   # правка жива
    assert ev.triage_status == "done"


async def test_text_undo_does_not_overwrite_a_later_hide(sqlite_db):
    await add_event(1, "v0", status="done")
    edit = await w.update_event(1, ctx(), content_text="v1")
    await w.hide_event(1, ctx())
    await w.undo(edit["audit_id"], ctx())
    ev = await event()
    assert ev.content_text == "v0"
    assert ev.triage_status == "hidden"                             # скрытие не откачено


async def test_unhide_undo_hides_again_and_refuses_after_later_change(sqlite_db):
    await add_event(1, "x", status="done")
    await w.hide_event(1, ctx())
    shown = await w.unhide_event(1, ctx())
    await w.undo(shown["audit_id"], ctx())
    assert (await event()).triage_status == "hidden"

    await add_event(2, "y", status="done")
    await w.hide_event(2, ctx())
    shown = await w.unhide_event(2, ctx())
    await w.hide_event(2, ctx())
    with pytest.raises(UndoRefused, match="force"):
        await w.undo(shown["audit_id"], ctx())


# ─── занятые воркером события ────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["processing", "media_pending"])
async def test_busy_event_is_not_edited_or_hidden(sqlite_db, status):
    await add_event(1, "x", status=status)
    with pytest.raises(EventBusy, match=status):
        await w.update_event(1, ctx(), content_text="y")
    with pytest.raises(EventBusy):
        await w.hide_event(1, ctx())
    assert (await event()).content_text == "x"
    assert await audit_rows() == []


async def test_busy_event_blocks_undo_too(sqlite_db):
    await add_event(1, "v0", status="done")
    edit = await w.update_event(1, ctx(), content_text="v1")
    async with get_session() as s:
        (await s.get(EventRow, 1)).triage_status = "processing"
    with pytest.raises(EventBusy):
        await w.undo(edit["audit_id"], ctx())


@pytest.mark.parametrize(("status", "expected"), [
    ("done", "pending"), ("error", "pending"), ("pending", "pending"),
    ("superseded", "superseded"), ("dead", "dead")])
async def test_text_edit_requeues_only_from_done_or_error(sqlite_db, status, expected):
    await add_event(1, "x", status=status)
    await w.update_event(1, ctx(), content_text="y")
    assert (await event()).triage_status == expected


# ─── слияние сущностей ──────────────────────────────────────────────────────


async def _names() -> dict[int, str]:
    async with get_session() as s:
        return {e.id: e.name for e in (await s.execute(select(EntityRow))).scalars()}


async def test_entity_merge_audits_report_and_undo_restores_the_duplicate(sqlite_db):
    keep, dup, other = await add_entity("Keep"), await add_entity("Dup"), await add_entity("O")
    await w.entity_add_alias(dup, "telegram", "user:9", ctx())
    await w.relationship_set(dup, other, "friend_of", ctx())
    res = await w.entity_merge(keep, [dup], "same person", ctx("codex"), dry_run=False)
    assert res["keep_id"] == keep and res["merged"] == [dup] and res["counts"]
    assert dup not in await _names()
    async with get_session() as s:
        alias = (await s.execute(select(EntityAliasRow))).scalar_one()
        assert alias.entity_id == keep                       # алиас переехал к победителю
    row = (await audit_rows())[-1]
    assert (row.tool, row.target_kind, row.client) == ("entity_merge", "merge", "codex")
    assert row.before["drop_ids"] == [dup]                   # весь MergeReport

    out = await w.entity_unmerge(res["audit_id"], ctx())
    assert out["ok"] is True
    assert (await _names())[dup] == "Dup"
    async with get_session() as s:
        alias = (await s.execute(select(EntityAliasRow))).scalar_one()
        assert alias.entity_id == dup                        # вернулся на прежнее место
        rel = (await s.execute(select(RelationshipRow))).scalar_one()
        assert rel.subject_entity_id == dup


async def test_merge_undo_via_generic_undo_and_refusals(sqlite_db):
    keep, dup = await add_entity("Keep"), await add_entity("Dup")
    res = await w.entity_merge(keep, [dup], "same person", ctx(), dry_run=False)
    await w.entity_rename(keep, "Renamed", ctx())
    with pytest.raises(UndoRefused, match="force"):
        await w.undo(res["audit_id"], ctx())
    await w.undo(res["audit_id"], ctx(), force=True)
    assert await _names() == {keep: "Keep", dup: "Dup"}
    with pytest.raises(UndoRefused, match="already undone"):
        await w.entity_unmerge(res["audit_id"], ctx())


async def test_merge_undo_refused_when_the_dropped_id_is_taken(sqlite_db):
    keep, dup = await add_entity("Keep"), await add_entity("Dup")
    res = await w.entity_merge(keep, [dup], "same person", ctx(), dry_run=False)
    await add_entity("Newcomer", entity_id=dup)
    with pytest.raises(UndoRefused, match="заняты"):
        await w.undo(res["audit_id"], ctx())


async def test_merge_validation_and_unmerge_of_other_tool(sqlite_db):
    keep = await add_entity("Keep")
    with pytest.raises(MergeError):
        await w.entity_merge(keep, [keep], "self merge", ctx(), dry_run=False)
    with pytest.raises(MergeError, match="не найдена"):
        await w.entity_merge(keep, [999], "ghost", ctx(), dry_run=False)
    assert await audit_rows() == []
    hid = await add_event(1, "x")
    shown = await w.hide_event(hid, ctx())
    with pytest.raises(ValueError, match="not entity_merge"):
        await w.entity_unmerge(shown["audit_id"], ctx())


async def test_merge_defaults_to_a_dry_run_that_changes_nothing(sqlite_db):
    keep, dup, other = await add_entity("Keep"), await add_entity("Dup"), await add_entity("O")
    await w.entity_add_alias(dup, "telegram", "user:9", ctx())
    await w.relationship_set(dup, other, "friend_of", ctx())
    before_audit = len(await audit_rows())
    out = await w.entity_merge(keep, [dup], "same person", ctx())
    assert out["dry_run"] is True and out["audit_id"] is None
    assert out["keep"] == {"id": keep, "name": "Keep"}
    assert out["drops"] == [{"id": dup, "name": "Dup"}]
    assert out["counts"] and out["blockers"] == [] and out["would_be_refused"] is False
    assert await _names() == {keep: "Keep", dup: "Dup", other: "O"}
    async with get_session() as s:
        aliases = (await s.execute(select(EntityAliasRow))).scalars().all()
        assert [a.entity_id for a in aliases] == [dup]               # откатилось
    assert len(await audit_rows()) == before_audit                   # журнал не тронут


async def test_merge_of_the_owner_needs_force(sqlite_db):
    from vera_mcp.merge_guard import MergeBlocked
    from vera_shared.projects.rules import OWNER_TG_ID

    owner, dup = await add_entity("Owner"), await add_entity("Owner duplicate")
    async with get_session() as s:
        s.add(EntityAliasRow(entity_id=owner, source="telegram", identifier=f"user:{OWNER_TG_ID}"))
    preview = await w.entity_merge(owner, [dup], "same person", ctx())
    assert preview["would_be_refused"] is True and "owner" in preview["blockers"][0]
    assert preview["counts"] is None
    with pytest.raises(MergeBlocked, match="force=true"):
        await w.entity_merge(owner, [dup], "same person", ctx(), dry_run=False)
    assert dup in await _names()
    forced = await w.entity_merge(owner, [dup], "same person", ctx(), dry_run=False, force=True)
    assert forced["merged"] == [dup]
    assert dup not in await _names()


async def test_merge_of_an_entity_with_identity_nodes_needs_force(sqlite_db):
    from vera_mcp.merge_guard import MergeBlocked
    from vera_shared.db.models_graph import IdentityNodeRow

    keep, dup = await add_entity("Keep"), await add_entity("Dup")
    async with get_session() as s:
        s.add(IdentityNodeRow(type="style", label="tone", listener_entity_id=dup))
    with pytest.raises(MergeBlocked, match="identity nodes"):
        await w.entity_merge(keep, [dup], "same person", ctx(), dry_run=False)
    forced = await w.entity_merge(keep, [dup], "same person", ctx(), dry_run=False, force=True)
    assert forced["audit_id"]


# ─── remember после undo ────────────────────────────────────────────────────


async def test_remember_after_undo_revives_the_hidden_event_and_audits_it(sqlite_db):
    from vera_shared.memory.remember import _content_hash

    text = "a fact that was undone"
    async with get_session() as s:
        s.add(EventRow(id=3, source="claude", source_event_id=_content_hash(text),
                       content_text=text, occurred_at=datetime(2026, 1, 1),
                       triage_status="hidden", triage_metadata={"hidden_prev_status": "done"}))
    res = await w.remember(text, ctx("codex"))
    assert (res["event_id"], res["deduped"], res["unhidden"]) == (3, True, True)
    assert res["audit_id"]
    assert (await event(3)).triage_status == "done"                  # вернулся, не молчит
    (row,) = await audit_rows()
    assert (row.tool, row.target_id, row.client) == ("remember", 3, "codex")

    await w.undo(res["audit_id"], ctx())                             # откат снова скрывает
    assert (await event(3)).triage_status == "hidden"
    again = await w.remember(text, ctx())
    assert again["unhidden"] is True and (await event(3)).triage_status == "done"


async def test_remember_exact_duplicate_of_a_visible_event_stays_a_noop(sqlite_db):
    from vera_shared.memory.remember import _content_hash

    text = "a visible fact"
    async with get_session() as s:
        s.add(EventRow(id=4, source="claude", source_event_id=_content_hash(text),
                       content_text=text, occurred_at=datetime(2026, 1, 1),
                       triage_status="done"))
    res = await w.remember(text, ctx())
    assert (res["deduped"], res["unhidden"], res["audit_id"]) == (True, False, None)


# ─── новая тройка связи: INSERT ... ON CONFLICT ─────────────────────────────


async def test_relationship_set_survives_a_concurrent_insert_of_the_same_triple(
        sqlite_db, monkeypatch):
    from vera_shared.graph import edit as graph_edit

    a, b = await add_entity("A"), await add_entity("B")
    first = await w.relationship_set(a, b, "friend_of", ctx(), fact="winner", confidence=0.4)
    real = graph_edit._locked_triple
    calls = []

    async def blind_first(s, subject_id, object_id, predicate):
        calls.append(1)
        return None if len(calls) == 1 else await real(s, subject_id, object_id, predicate)

    monkeypatch.setattr(graph_edit, "_locked_triple", blind_first)
    second = await w.relationship_set(a, b, "friend_of", ctx(), fact="loser", confidence=0.9)
    assert second["relationship_id"] == first["relationship_id"] and second["created"] is False
    row = await rel_row(first["relationship_id"])
    assert (row.fact, row.confidence) == ("loser", 0.9)
    async with get_session() as s:
        assert len((await s.execute(select(RelationshipRow))).scalars().all()) == 1

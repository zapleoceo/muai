"""Применение плана чистки: конфликт уникальности, откат изменённой строки, автор."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from vera_shared.graph import repo
from vera_shared.graph.rel_cleanup import build_plan
from vera_shared.graph.rel_cleanup_apply import apply_plan, undo_report

NAMES = {"2": ["Anna Lee"], "3": ["Олег Сидоров"]}


def test_message_author_counts_as_named_in_soft_fact_check():
    rel = {"id": 1, "subject_entity_id": 3, "predicate": "works_at", "object_entity_id": 2,
           "confidence": 0.9, "fact": "я работаю в Anna Lee", "is_current": True,
           "derived_from_event_id": 1, "subject_name": "Олег Сидоров",
           "subject_type": "person", "object_name": "Anna Lee",
           "object_type": "organization", "event_source": "telegram",
           "event_meta": '{"sender_id": 77}'}
    snap = {"owner_id": 5, "names": NAMES, "aliases": {"telegram:user:77": 3},
            "relationships": [rel]}
    assert build_plan(snap) == []
    snap["aliases"] = {}
    assert [a["rule"] for a in build_plan(snap)] == ["fact_mismatch"]


def _convert_plan(a, b, rel_id=1):
    return {"version": 1, "actions": [{
        "id": rel_id, "action": "convert", "rule": "convert_inverse", "rel_id": rel_id,
        "before": {"subject_entity_id": a, "predicate": "reports_to",
                   "object_entity_id": b, "is_current": True},
        "after": {"subject_entity_id": b, "predicate": "boss_of",
                  "object_entity_id": a, "is_current": True}}]}


async def _legacy_reports_to(sqlite_db, count=1):
    a = await repo.upsert_entity(type="person", name="Ivan Petrov", source="s", identifier="a")
    others = [await repo.upsert_entity(type="person", name=f"Anna Lee {i}", source="s",
                                       identifier=f"o{i}") for i in range(count)]
    async with sqlite_db() as s:
        for other in others:
            await s.execute(text(
                "INSERT INTO relationships (subject_entity_id, predicate, object_entity_id, "
                "confidence, first_seen_at, last_seen_at, is_current) VALUES "
                "(:a, 'reports_to', :b, 0.9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"),
                {"a": a, "b": other})
    return a, others


async def _state(sqlite_db):
    async with sqlite_db() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT subject_entity_id, predicate, object_entity_id, is_current "
            "FROM relationships ORDER BY id"))).all()]


@pytest.mark.asyncio
async def test_apply_skips_action_when_canonical_triple_was_taken_meanwhile(sqlite_db, tmp_path):
    a, (b, c) = await _legacy_reports_to(sqlite_db, 2)
    async with sqlite_db() as s:
        await s.execute(text(
            "INSERT INTO relationships (subject_entity_id, predicate, object_entity_id, "
            "confidence, first_seen_at, last_seen_at, is_current) VALUES "
            "(:b, 'boss_of', :a, 0.9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"),
            {"a": a, "b": b})
    plan_doc = _convert_plan(a, b, rel_id=1)
    plan_doc["actions"] += _convert_plan(a, c, rel_id=2)["actions"]
    result = await apply_plan(plan_doc, tmp_path / "r.json")
    assert [e["rel_id"] for e in result["entries"]] == [2]
    assert "уникальности" in result["skipped"][0]["reason"]
    state = await _state(sqlite_db)
    assert (a, "reports_to", b, 1) in state
    assert (c, "boss_of", a, 1) in state


@pytest.mark.asyncio
async def test_undo_skips_row_changed_after_apply(sqlite_db, tmp_path):
    a, (b,) = await _legacy_reports_to(sqlite_db)
    report = tmp_path / "r.json"
    await apply_plan(_convert_plan(a, b), report)
    async with sqlite_db() as s:
        await s.execute(text("UPDATE relationships SET predicate = 'friend_of'"))
    assert await undo_report(report) == 0
    entry = json.loads(report.read_text("utf-8"))["entries"][0]
    assert entry["undone"] and "изменилась" in entry["note"]

"""Чистка связей: план по срезу (чистые функции) и запись/откат на SQLite."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from vera_shared.graph import repo
from vera_shared.graph.rel_cleanup import (
    build_plan,
    plan_document,
    weak_name_candidates,
)
from vera_shared.graph.rel_cleanup_apply import PlanError, apply_plan, undo_report

NAMES = {"1": ["Ivan Petrov"], "2": ["Anna Lee"], "3": ["Олег"], "4": ["Maria Kim"],
         "5": ["Owner Person"]}


WITH_OLEG = "Ivan Petrov и Олег"


def row(rel_id, s, p, o, *, conf=0.9, fact=None, event=1, current=True):
    fact = fact if fact is not None else "Ivan Petrov Anna Lee Maria Kim"
    nm = {k: v[0] for k, v in NAMES.items()}
    return {"id": rel_id, "subject_entity_id": s, "predicate": p, "object_entity_id": o,
            "confidence": conf, "fact": fact, "is_current": current,
            "derived_from_event_id": event, "subject_name": nm[str(s)],
            "subject_type": "person", "object_name": nm[str(o)], "object_type": "person"}


def plan(*rows, owner=5):
    return build_plan({"owner_id": owner, "relationships": list(rows), "names": NAMES})


def by_rule(actions):
    return {a["rel_id"]: a["rule"] for a in actions}


def test_soft_phase_retires_fact_mismatch_but_not_single_token():
    actions = plan(row(1, 1, "coworker_of", 3, fact=WITH_OLEG),
                   row(2, 1, "coworker_of", 2, fact="nothing"),
                   row(3, 1, "coworker_of", 4))
    assert by_rule(actions) == {2: "fact_mismatch"}


def test_weak_name_candidates_are_left_for_the_verifier():
    snap = {"owner_id": 5, "names": NAMES, "relationships": [
        row(1, 1, "coworker_of", 3, fact=WITH_OLEG),
        row(2, 1, "coworker_of", 3, fact="nothing"),
        row(3, 1, "coworker_of", 4), row(4, 1, "coworker_of", 3, event=None)]}
    assert [r["id"] for r in weak_name_candidates(snap)] == [1, 2]  # факт слабой связи не судит


def test_manual_edges_not_judged_by_extraction_rules():
    assert plan(row(1, 1, "coworker_of", 3, event=None, fact="")) == []


def test_owner_single_token_allowed_via_first_person():
    names = {**NAMES, "5": ["Дима"]}
    rel = row(1, 5, "works_at", 2, fact="я работаю с Anna Lee")
    rel["object_type"] = "organization"
    rel["object_name"] = "Anna Lee"
    rel["subject_name"] = "Дима"
    assert build_plan({"owner_id": 5, "relationships": [rel], "names": names}) == []


def test_symmetric_duplicate_keeps_stronger():
    actions = plan(row(1, 1, "coworker_of", 2, conf=0.6), row(2, 2, "coworker_of", 1, conf=0.9))
    assert {a["rel_id"]: a["rule"] for a in actions if a["action"] == "retire"} == {
        1: "symmetric_duplicate"}
    assert actions[0]["keep_id"] == 2


def test_contradiction_and_inverse_pair():
    actions = plan(row(1, 1, "boss_of", 2, conf=0.9), row(2, 2, "boss_of", 1, conf=0.7),
                   row(3, 1, "boss_of", 4), row(4, 4, "reports_to", 1, conf=0.8))
    assert by_rule(actions) == {2: "contradiction", 4: "inverse_duplicate"}


def test_lone_inverse_is_converted_unless_target_taken():
    actions = plan(row(1, 1, "reports_to", 2), row(2, 4, "reports_to", 2),
                   row(3, 2, "boss_of", 4, current=False))
    kinds = {a["rel_id"]: (a["action"], a["rule"]) for a in actions}
    assert kinds[1] == ("convert", "convert_inverse")
    assert kinds[2] == ("skip", "convert_blocked")
    assert actions[0]["after"] == {"subject_entity_id": 2, "predicate": "boss_of",
                                   "object_entity_id": 1, "is_current": True}


def test_plan_document_counts():
    doc = plan_document(plan(row(1, 1, "coworker_of", 2, fact="x"), row(2, 1, "reports_to", 2)),
                        "t")
    assert doc["counts"] == {"fact_mismatch": 1, "convert_inverse": 1}
    assert doc["to_apply"] == 2 and doc["phase"] == "soft"


async def _seed(sqlite_db):
    ids = [await repo.upsert_entity(type="person", name=n, source="s", identifier=n)
           for n in ("Ivan Petrov", "Anna Lee")]
    await repo.upsert_relationship(subject_entity_id=ids[0], object_entity_id=ids[1],
                                   predicate="reports_to", fact="x", confidence=0.9,
                                   derived_from_event_id=None)
    return ids


async def _state(sqlite_db):
    async with sqlite_db() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT subject_entity_id, predicate, object_entity_id, is_current "
            "FROM relationships ORDER BY id"))).all()]


@pytest.mark.asyncio
async def test_apply_writes_report_first_and_undo_restores(sqlite_db, tmp_path):
    a, b = await _seed(sqlite_db)
    assert await _state(sqlite_db) == [(b, "boss_of", a, True)]
    legacy = {"version": 1, "actions": [{
        "id": 1, "action": "convert", "rule": "convert_inverse", "rel_id": 1,
        "before": {"subject_entity_id": b, "predicate": "boss_of",
                   "object_entity_id": a, "is_current": True},
        "after": {"subject_entity_id": a, "predicate": "reports_to",
                  "object_entity_id": b, "is_current": True}}]}
    report = tmp_path / "rollback.json"
    result = await apply_plan(legacy, report)
    assert len(result["entries"]) == 1
    assert await _state(sqlite_db) == [(a, "reports_to", b, True)]
    assert json.loads(report.read_text("utf-8"))["entries"][0]["undone"] is False

    assert await undo_report(report) == 1
    assert await _state(sqlite_db) == [(b, "boss_of", a, True)]
    assert await undo_report(report) == 0


@pytest.mark.asyncio
async def test_apply_refuses_without_report_or_over_existing(sqlite_db, tmp_path):
    plan_doc = {"version": 1, "actions": []}
    with pytest.raises(PlanError):
        await apply_plan(plan_doc, None)
    existing = tmp_path / "r.json"
    existing.write_text("{}")
    with pytest.raises(PlanError):
        await apply_plan(plan_doc, existing)


@pytest.mark.asyncio
async def test_apply_skips_rows_changed_after_plan(sqlite_db, tmp_path):
    a, b = await _seed(sqlite_db)
    stale = {"version": 1, "actions": [{
        "id": 1, "action": "retire", "rule": "weak_name", "rel_id": 1,
        "before": {"subject_entity_id": a, "predicate": "boss_of",
                   "object_entity_id": b, "is_current": True},
        "after": {"subject_entity_id": a, "predicate": "boss_of",
                  "object_entity_id": b, "is_current": False}}]}
    result = await apply_plan(stale, tmp_path / "r.json")
    assert result["entries"] == [] and len(result["skipped"]) == 1
    assert bool((await _state(sqlite_db))[0][3])

"""scripts/quarantine_junk_rels.py — мусорные связи прячутся обратимо.

Держит три обещания: аудит ничего не пишет (dry-run безопасен на проде),
пометка `is_current = false` убирает связь из карточки и графа, но строка
остаётся, и отчёт позволяет вернуть всё назад.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from vera_shared.graph import repo

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quarantine_junk_rels.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("quarantine_junk_rels", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed() -> dict[str, int]:
    olga = await repo.upsert_entity(type="person", name="Olga Kryachko",
                                    source="gmail", identifier="olga@x")
    jira = await repo.upsert_entity(type="person", name="Ольга Крячко (JIRA)",
                                    source="gmail", identifier="jira@x")
    artem = await repo.upsert_entity(type="person", name="Artem Belov",
                                     source="gmail", identifier="artem@x")
    org = await repo.upsert_entity(type="organization", name="IT STEP",
                                   source="gmail", identifier="itstep@x")
    for s, p, o in [(artem, "works_at", org),      # нормальная
                    (jira, "works_at", olga),      # сама с собой
                    (olga, "works_at", artem)]:    # объект — персона
        await repo.upsert_relationship(subject_entity_id=s, object_entity_id=o,
                                       predicate=p, fact="f", confidence=0.9)
    return {"olga": olga, "artem": artem, "org": org}


async def _current(get_session) -> dict[int, bool]:
    async with get_session() as s:
        rows = (await s.execute(text("SELECT id, is_current FROM relationships"))).all()
    return {r[0]: bool(r[1]) for r in rows}


@pytest.mark.asyncio
async def test_audit_is_read_only(sqlite_db, script):
    await _seed()
    before = await _current(sqlite_db)

    report = await script.audit(page=2)       # страница меньше выборки — keyset

    assert (report["total"], report["flagged"], report["kept"]) == (3, 2, 1)
    assert report["reasons"] == {"self_loop": 1, "type_mismatch": 1}
    assert await _current(sqlite_db) == before
    assert all(before.values())


@pytest.mark.asyncio
async def test_mark_hides_from_graph_and_restore_brings_back(sqlite_db, script, tmp_path):
    ids = await _seed()
    report = await script.audit()
    path = tmp_path / "junk.json"
    path.write_text(json.dumps(report), "utf-8")
    flagged = script.report_ids(path)

    assert await script.mark(flagged, current=False) == 2
    state = await _current(sqlite_db)
    assert sum(state.values()) == 1 and len(state) == 3      # строки на месте

    card = await repo.list_relationships(ids["olga"])
    assert card == []                                        # мусор не в карточке
    snap = await repo.graph_snapshot(min_degree=1, limit=50)
    assert {(e["source"], e["target"]) for e in snap["edges"]} == {(ids["artem"], ids["org"])}
    assert (await script.audit())["flagged"] == 0            # повторный прогон пуст

    assert await script.mark(script.report_ids(path), current=True) == 2
    assert all((await _current(sqlite_db)).values())


@pytest.mark.asyncio
async def test_mark_nothing(sqlite_db, script):
    assert await script.mark([], current=False) == 0

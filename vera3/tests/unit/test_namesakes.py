"""Одиночное имя резолвится только в круге разговора; чистка прошлых связей с тёзкой вне круга:
план, применение, откат. Данные синтетические.

Образец: «Дима» — десятки людей в графе, единственный по точному имени оказался автором
десяти сообщений в публичном чате, и связи из переписки владельца уехали к нему.
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import RelationshipRow
from vera_shared.graph import repo
from vera_shared.graph.rel_cleanup_apply import PlanError, apply_plan, undo_report
from vera_shared.graph.rel_extract import extract_and_store
from vera_shared.links.circle import event_circle, resolve_short_name
from vera_shared.links.namesake_plan import (
    RULE_OUT_OF_CIRCLE,
    RULE_REPOINT,
    build_namesake_plan,
    load_rows,
    namesake_document,
)

pytestmark = pytest.mark.asyncio
OWNER_TG = "100"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER_TG)


async def person(name: str, tg: str) -> int:
    return await repo.upsert_entity(type="person", name=name, source="telegram", identifier=f"user:{tg}")


async def event(gs, n: int, chat: str, sender: str, body: str) -> int:
    async with gs() as s:
        row = EventRow(source="telegram", source_event_id=f"n{n}", content_text=body,
                       occurred_at=datetime(2026, 9, n), triage_status="done",
                       metadata_={"chat_type": "chat", "chat_id": chat, "sender_id": sender})
        s.add(row)
        await s.flush()
        return row.id


async def rel(subject: int, predicate: str, obj: int, event_id: int) -> None:
    await repo.upsert_relationship(subject_entity_id=subject, object_entity_id=obj,
                                   predicate=predicate, confidence=0.8, fact="x",
                                   derived_from_event_id=event_id)


@pytest_asyncio.fixture
async def world(sqlite_db):
    gs = sqlite_db
    w = {"gs": gs, "owner": await person("Dmitry Testov", OWNER_TG), "lisa": await person("Лиза Ветрова", "200"),
         "namesake": await person("Дима", "999"), "andrey": await person("Андрей", "888")}
    w["org"] = await repo.upsert_entity(type="organization", name="Acme", source="telegram",
                                        identifier="chat:-9")
    w["mine"] = await event(gs, 1, "-1005001", "200", "Дима, пришли файл")
    w["theirs"] = await event(gs, 2, "-1009", "999", "я здесь пишу про политику")
    await rel(w["namesake"], "boss_of", w["lisa"], w["mine"])
    await rel(w["namesake"], "client_of", w["org"], w["theirs"])
    await rel(w["andrey"], "works_at", w["org"], w["mine"])
    return w


async def current(gs) -> set[tuple[int, str, int]]:
    async with gs() as s:
        rows = (await s.execute(select(RelationshipRow).where(RelationshipRow.is_current))).scalars()
        return {(r.subject_entity_id, r.predicate, r.object_entity_id) for r in rows}


async def test_circle_contains_authors_owner_and_recipients(world):
    circle = await event_circle(world["mine"])
    assert {world["lisa"], world["owner"]} <= circle.primary
    assert world["namesake"] not in circle.primary


async def test_short_name_resolves_to_the_owner_not_the_namesake(world):
    circle = await event_circle(world["mine"])
    assert await resolve_short_name("Дима", circle) == world["owner"]
    assert await resolve_short_name("Андрей", circle) is None      # в круге нет ни одного Андрея
    # в его публичном чате в круге двое Дим (он сам и владелец): неоднозначно — связи нет
    assert await resolve_short_name("Дима", await event_circle(world["theirs"])) is None


async def test_plan_repoints_to_the_owner_and_retires_the_orphan(world):
    rows = await load_rows([world["namesake"], world["andrey"]])
    actions = await build_namesake_plan(rows)
    by = {a["rule"]: a for a in actions}
    assert set(by) == {RULE_REPOINT, RULE_OUT_OF_CIRCLE}
    assert by[RULE_REPOINT]["after"]["subject_entity_id"] == world["owner"]
    assert by[RULE_OUT_OF_CIRCLE]["after"]["is_current"] is False
    # связь «тёзки» из его собственного публичного чата не тронута
    assert all("client_of" not in a["brief"] for a in actions)
    doc = namesake_document(actions, "entities:test")
    assert doc["to_apply"] == 2 and doc["by_rule"] == {RULE_REPOINT: 1, RULE_OUT_OF_CIRCLE: 1}


async def test_apply_then_undo_restores_every_row(world, tmp_path):
    before = await current(world["gs"])
    doc = namesake_document(await build_namesake_plan(await load_rows([world["namesake"], world["andrey"]])), "t")
    report = tmp_path / "rollback.json"
    result = await apply_plan(json.loads(json.dumps(doc)), report)
    assert len(result["entries"]) == 2
    after = await current(world["gs"])
    assert (world["owner"], "boss_of", world["lisa"]) in after
    assert (world["namesake"], "boss_of", world["lisa"]) not in after
    assert (world["andrey"], "works_at", world["org"]) not in after
    assert (world["namesake"], "client_of", world["org"]) in after
    assert await undo_report(report) == 2
    assert await current(world["gs"]) == before


async def test_apply_refuses_without_a_report(world):
    with pytest.raises(PlanError):
        await apply_plan({"version": 1, "actions": []}, None)


async def test_extraction_resolves_a_short_name_inside_the_circle(world):
    reply = json.dumps({"relationships": [
        {"subject": "Дима", "predicate": "boss_of", "object": "Лиза Ветрова",
         "fact": "Дима начальник Лиза Ветрова", "confidence": 0.9}]})
    with patch("vera_shared.graph.rel_extract.chat_async", AsyncMock(return_value=(reply, {}))), \
         patch("vera_shared.graph.rel_extract.judge_batch", AsyncMock(return_value=[None])), \
         patch("vera_shared.graph.rel_extract.upsert_relationship", AsyncMock(return_value=True)) as up:
        out = await extract_and_store(world["mine"], "Дима, пришли файл " * 3)
    assert out.inserted == 1
    assert up.await_args.kwargs["subject_entity_id"] == world["owner"]
    assert up.await_args.kwargs["subject_entity_id"] != world["namesake"]

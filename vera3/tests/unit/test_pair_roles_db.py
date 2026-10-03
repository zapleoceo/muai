"""Вывод ролей пары поверх SQLite с поддельным брокером: пакет из `event_entities`, запись и
пересчёт по хэшу, dry-run, сбои, очередь, надстройка над карточкой и «это неверно».
Образец — отношения «начальник — подчинённый», которых ни одно сообщение не называет прямо."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import PairStatsRow
from vera_shared.db.models_pair_roles import PairRoleInferenceRow, PairRoleRunRow
from vera_shared.graph import connections, repo
from vera_shared.graph.connection_actions import reject_inferred
from vera_shared.graph.pair_roles import (
    MIN_MESSAGES,
    build_pair_evidence,
    infer_pair,
    run_cycle,
)
from vera_shared.graph.pair_roles_prompt import pack_payload
from vera_shared.graph.pair_roles_store import roles_of
from vera_shared.graph.pair_stats import PairStats
from vera_shared.journal.undo import undo_entry
from vera_shared.links import index
from vera_shared.links.context import ContextBuilder
from vera_shared.links.nicknames import add_nickname
from vera_shared.llm.client import LLMCallFailed

pytestmark = pytest.mark.asyncio
OWNER_TG = "100"
QUOTE = "Прошу подготовить отчёт до пятницы"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER_TG)


def reply(*roles: dict, summary: str = "руководитель и подчинённый") -> tuple[str, dict]:
    return (json.dumps({"roles": list(roles), "relationship_summary": summary}, ensure_ascii=False),
            {"cost_usd": 0.012, "model": "fake-model"})


def boss_role(quote: str = QUOTE, subject: str = "B") -> dict:
    # вторая цитата — слова подчинённого: подтверждение, без него роль была бы самоутверждением
    return {"predicate": "boss_of", "subject": subject, "confidence": 0.92,
            "rationale": "поручения, отчёт перед ним, обращение по имени-отчеству",
            "quotes": [quote, "Виктор Павлович, отчёт готов"], "joke_or_irony_only": False}


async def person(name: str, tg: str) -> int:
    return await repo.upsert_entity(type="person", name=name, source="telegram", identifier=f"user:{tg}")


async def message(gs, n: int, body: str, *, chat: str, sender: str, kind: str = "user",
                  direction: str | None = None) -> None:
    async with gs() as s:
        s.add(EventRow(source="telegram", source_event_id=f"p{n}", content_text=body,
                       occurred_at=datetime(2026, 3 + n // 28, 1 + n % 28), triage_status="done",
                       metadata_={"chat_type": kind, "chat_id": chat, "sender_id": sender,
                                  **({"direction": direction} if direction else {})}))


async def rebuild_links(gs) -> None:
    builder = ContextBuilder()
    await builder.build([])
    async with gs() as s:
        rows = list((await s.execute(select(EventRow))).scalars())
    await index.index_events(rows, await index.load_resources(builder.owner), builder)


@pytest_asyncio.fixture
async def world(sqlite_db):
    gs = sqlite_db
    w = {"gs": gs, "owner": await person("Игорь Тестов", OWNER_TG),
         "boss": await person("Виктор Кронов", "300"), "lisa": await person("Лиза Ветрова", "200")}
    async with gs() as s:
        s.add(PairStatsRow(entity_a=w["owner"], entity_b=w["boss"], dm_msgs=8, dm_days=8, active_days=20))
    for n in range(4):
        await message(gs, n, "Виктор Павлович, отчёт готов, отправил", chat="300", sender=OWNER_TG,
                      direction="sent")
        await message(gs, 10 + n, f"{QUOTE}, пункт {n}", chat="300", sender="300", direction="received")
    await add_nickname(w["boss"], "ВП", scope_kind="chats", scope_ids=["telegram:-1007"])
    await message(gs, 20, "ВП просил ознакомиться с регламентом", chat="-1007", sender="200", kind="chat")
    await rebuild_links(gs)
    return w


async def stored_rows(gs) -> tuple[list, list]:
    async with gs() as s:
        return (list((await s.execute(select(PairRoleInferenceRow))).scalars()),
                list((await s.execute(select(PairRoleRunRow))).scalars()))


def fake_llm(*answers):
    return patch("vera_shared.graph.pair_roles.chat_async", AsyncMock(side_effect=list(answers)))


async def test_evidence_pack_uses_event_entities_and_third_party_mentions(world):
    ev = await build_pair_evidence(world["owner"], world["boss"], PairStats(dm_msgs=8, active_days=20))
    payload = pack_payload(ev)
    kinds = {m["kind"] for m in payload["messages"]}
    assert kinds == {"dm", "mention"}
    mention = next(m for m in payload["messages"] if m["kind"] == "mention")
    assert (mention["from"], mention["about"]) == ("X", "B") and "ВП" in mention["text"]
    assert payload["people"]["B"]["nicknames"] == ["ВП"] and payload["people"]["A"]["is_owner"] is True
    assert {m["from"] for m in payload["messages"] if m["kind"] == "dm"} == {"A", "B"}
    assert payload["signals"]["pair_stats"]["dm_msgs"] == 8


async def test_role_is_stored_with_quotes_and_shown_in_the_card(world):
    with fake_llm(reply(boss_role())) as llm:
        result = await infer_pair(world["boss"], world["owner"])           # порядок пары неважен
    assert llm.await_count == 1 and (result.entity_a, result.entity_b) == (world["owner"], world["boss"])
    (row,), (run,) = await stored_rows(world["gs"])
    assert (row.predicate, row.direction, row.quotes) == (
        "boss_of", "b_to_a", [QUOTE, "Виктор Павлович, отчёт готов"])
    assert run.roles_found == 1 and run.cost_usd == pytest.approx(0.012) and run.summary
    card = (await connections.entity_connections(world["owner"]))[0]
    assert card["main"]["predicate"] == "boss_of" and card["main"]["direction"] == "in"
    assert card["main"]["source"] == "history" and card["main"]["source_label"] == "выведено из переписки"
    assert card["main"]["quotes"][0] == QUOTE and card["main"]["inferred"] is True
    edge = (await connections.connections_among([world["owner"], world["boss"]]))[0]
    assert (edge["predicate"], edge["source"], edge["source"] == "history") == ("boss_of", "history", True)


async def test_unchanged_pack_does_not_call_the_model_again_but_new_messages_do(world):
    with fake_llm(reply(boss_role()), reply(boss_role())) as llm:
        await infer_pair(world["owner"], world["boss"])
        again = await infer_pair(world["owner"], world["boss"])
        assert llm.await_count == 1 and again.skipped == "пакет улик не изменился"
        await message(world["gs"], 25, f"{QUOTE}, ещё раз", chat="300", sender="300", direction="received")
        await rebuild_links(world["gs"])
        await infer_pair(world["owner"], world["boss"])
        assert llm.await_count == 2
    assert len((await stored_rows(world["gs"]))[0]) == 1          # роли заменяются, не копятся


async def test_dry_run_calls_the_model_but_writes_nothing(world):
    with fake_llm(reply(boss_role())) as llm:
        result = await infer_pair(world["owner"], world["boss"], dry_run=True)
    assert llm.await_count == 1 and result.roles and result.cost_usd == pytest.approx(0.012)
    assert await stored_rows(world["gs"]) == ([], [])
    assert all(c["main"].get("source") != "history"
               for c in await connections.entity_connections(world["owner"]))


async def test_invented_quote_gives_no_role_but_the_pair_is_not_retried_every_cycle(world):
    invented = {**boss_role(quote="Я твой директор, слушай меня"),
                "quotes": ["Я твой директор, слушай меня", "Он мой начальник и всё решает"]}
    with fake_llm(reply(invented)):
        result = await infer_pair(world["owner"], world["boss"])
    assert result.roles == () and result.summary
    rows, runs = await stored_rows(world["gs"])
    assert rows == [] and runs[0].roles_found == 0


async def test_broker_failure_stores_nothing_and_a_bad_format_pauses_only_that_pair(world):
    with fake_llm(LLMCallFailed("broker 503")):
        failed = await infer_pair(world["owner"], world["boss"])
    assert failed.failed and "503" in failed.skipped and await stored_rows(world["gs"]) == ([], [])
    with fake_llm(("не json", {})):
        bad = await infer_pair(world["owner"], world["boss"])
    assert bad.bad_format and not bad.failed
    rows, (run,) = await stored_rows(world["gs"])
    assert rows == [] and run.failures == 1 and run.retry_after is not None and run.evidence_hash == ""
    with fake_llm(("не json", {})):
        await infer_pair(world["owner"], world["boss"])
    assert (await stored_rows(world["gs"]))[1][0].failures == 2
    with fake_llm(reply(boss_role())):                  # пауза закончилась, ответ верный: сбои сброшены
        ok = await infer_pair(world["owner"], world["boss"])
    assert ok.roles and (await stored_rows(world["gs"]))[1][0].failures == 0


async def test_too_little_evidence_is_skipped_without_calling_the_model(sqlite_db):
    a, b = await person("Игорь Тестов", OWNER_TG), await person("Лиза Ветрова", "200")
    await message(sqlite_db, 1, "привет", chat="200", sender="200", direction="received")
    await rebuild_links(sqlite_db)
    with fake_llm() as llm:
        result = await infer_pair(a, b)
    assert llm.await_count == 0 and str(MIN_MESSAGES) in result.skipped


async def test_cycle_judges_owner_pairs_first_and_stops_at_the_first_broker_failure(world):
    other = await person("Олег Громов", "400")
    async with world["gs"]() as s:
        s.add(PairStatsRow(entity_a=world["lisa"], entity_b=other, dm_msgs=500, active_days=300))
    with fake_llm(reply(boss_role())) as llm:
        done = await run_cycle(1)
    assert llm.await_count == 1 and [(r.entity_a, r.entity_b) for r in done] == [
        (world["owner"], world["boss"])]
    async with world["gs"]() as s:                       # чтобы второй проход начал заново
        for row in (await s.execute(select(PairRoleRunRow))).scalars():
            await s.delete(row)
    with fake_llm(LLMCallFailed("outage")) as llm:
        results = await run_cycle(5)
    assert len(results) == 1 and results[0].failed and llm.await_count == 1


async def test_manual_edit_and_owner_rejection_override_the_inferred_role(world):
    with fake_llm(reply(boss_role())):
        await infer_pair(world["owner"], world["boss"])
    # владелец отверг роль «начальник» именно у этой пары — выведенная пропадает, откат возвращает
    audit_id = await reject_inferred(world["owner"], world["boss"], "dashboard", "boss_of")
    cards = await connections.entity_connections(world["owner"])
    assert all(c["main"].get("source") != "history" for c in cards)
    async with world["gs"]() as s:
        await undo_entry(s, audit_id, "dashboard", force=False)
    assert (await connections.entity_connections(world["owner"]))[0]["main"]["source"] == "history"
    # ручная правка в противоположную сторону перекрывает вывод по истории
    await repo.upsert_relationship(subject_entity_id=world["owner"], object_entity_id=world["boss"],
                                   predicate="boss_of", confidence=1.0, derived_from_event_id=None)
    main = (await connections.entity_connections(world["owner"]))[0]["main"]
    assert main["manual"] is True and main["direction"] == "out" and main.get("source") != "history"
    assert (await roles_of(world["owner"]))[world["boss"]][0].predicate == "boss_of"   # данные остаются


async def test_a_bad_format_does_not_stop_the_cycle_but_a_broker_failure_does(world):
    other = await person("Олег Громов", "400")
    for n in range(6):
        await message(world["gs"], 40 + n, f"сообщение номер {n} с достаточным текстом", chat="400",
                      sender="400", direction="received")
    async with world["gs"]() as s:
        s.add(PairStatsRow(entity_a=world["owner"], entity_b=other, dm_msgs=8, dm_days=8, active_days=20))
    await rebuild_links(world["gs"])
    with fake_llm(("не json", {}), reply(boss_role())) as llm:
        done = await run_cycle(5)
    assert llm.await_count == 2 and [r.bad_format for r in done] == [True, False]
    async with world["gs"]() as s:
        for row in (await s.execute(select(PairRoleRunRow))).scalars():
            await s.delete(row)
    with fake_llm(LLMCallFailed("outage"), reply(boss_role())) as llm:
        done = await run_cycle(5)
    assert llm.await_count == 1 and done[0].failed and len(done) == 1


async def test_infer_pair_returns_the_trace_and_a_self_asserted_role_is_explained(world):
    own = {**boss_role(), "quotes": [QUOTE]}                 # только реплики самого начальника
    with fake_llm(reply(own)):
        result = await infer_pair(world["owner"], world["boss"], dry_run=True)
    assert result.roles == () and result.summary
    (t,) = result.trace
    assert t.self_assertion == "самоутверждение" and t.quotes[0].authors == ("B",)
    assert await stored_rows(world["gs"]) == ([], [])

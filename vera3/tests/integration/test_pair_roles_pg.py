"""Вывод ролей пары на живом Postgres: SELECT'ы пакета улик по `event_entities`, jsonb-цитаты,
замена ролей пары, очередь по `pair_stats` и надстройка над карточкой. Брокер поддельный.
Данные синтетические."""
from __future__ import annotations

import json
import os
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vera:test@localhost:5433/vera_test",
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)

OWNER = "100"
QUOTE = "Прошу подготовить отчёт до пятницы"


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-integration")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", OWNER)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import (  # noqa: F401
        models,
        models_graph,
        models_links,
        models_pair_roles,
        models_sources,
    )
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


async def _world(get_session) -> dict:
    from vera_shared.db.models import EventRow
    from vera_shared.db.models_graph import PairStatsRow
    from vera_shared.graph import repo
    from vera_shared.links import index
    from vera_shared.links.context import ContextBuilder

    owner = await repo.upsert_entity(type="person", name="Игорь Тестов", source="telegram", identifier=f"user:{OWNER}")
    boss = await repo.upsert_entity(type="person", name="Виктор Кронов", source="telegram", identifier="user:300")
    async with get_session() as s:
        s.add(PairStatsRow(entity_a=owner, entity_b=boss, dm_msgs=8, dm_days=8, active_days=20))
        for n in range(4):
            dm = {"chat_type": "user", "chat_id": "300"}
            s.add(EventRow(source="telegram", source_event_id=f"o{n}", content_text="Виктор Павлович, отчёт готов",
                           occurred_at=datetime(2026, 3, 1 + n), triage_status="done",
                           metadata_={**dm, "sender_id": OWNER, "direction": "sent"}))
            s.add(EventRow(source="telegram", source_event_id=f"b{n}", content_text=f"{QUOTE}, пункт {n}",
                           occurred_at=datetime(2026, 6, 1 + n), triage_status="done",
                           metadata_={**dm, "sender_id": "300", "direction": "received"}))
    builder = ContextBuilder()
    await builder.build([])
    async with get_session() as s:
        rows = list((await s.execute(select(EventRow))).scalars())
    await index.index_events(rows, await index.load_resources(builder.owner), builder)
    return {"owner": owner, "boss": boss}


def _llm():
    answer = json.dumps({"roles": [{"predicate": "boss_of", "subject": "B", "confidence": 0.9,
                                    "rationale": "поручения", "quotes": [QUOTE, "Виктор Павлович, отчёт готов"],
                                    "joke_or_irony_only": False}],
                         "relationship_summary": "руководитель и подчинённый"}, ensure_ascii=False)
    return patch("vera_shared.graph.pair_roles.chat_async",
                 AsyncMock(return_value=(answer, {"cost_usd": 0.01, "model": "fake"})))


@pytest.mark.asyncio
async def test_pack_is_built_from_event_entities_on_postgres(pg_db):
    from vera_shared.graph.pair_roles import build_pair_evidence
    from vera_shared.graph.pair_stats import PairStats

    w = await _world(pg_db)
    evidence = await build_pair_evidence(w["owner"], w["boss"], PairStats(dm_msgs=8, active_days=20))
    assert len(evidence.messages) == 8
    assert {m.author for m in evidence.messages} == {"A", "B"}
    assert {m.at[:7] for m in evidence.messages} == {"2026-03", "2026-06"}    # обе эпохи


@pytest.mark.asyncio
async def test_inference_roundtrip_queue_and_card_on_postgres(pg_db):
    from vera_shared.db.models_pair_roles import PairRoleInferenceRow, PairRoleRunRow
    from vera_shared.graph import connections
    from vera_shared.graph.pair_roles import infer_pair, run_cycle

    w = await _world(pg_db)
    with _llm() as llm:
        done = await run_cycle(5)
        again = await run_cycle(5)
    assert len(done) == 1 and llm.await_count == 1 and again == []          # вторая попытка: пара уже судима
    async with pg_db() as s:
        row = (await s.execute(select(PairRoleInferenceRow))).scalar_one()
        run = (await s.execute(select(PairRoleRunRow))).scalar_one()
    assert (row.predicate, row.direction, row.quotes) == (
        "boss_of", "b_to_a", [QUOTE, "Виктор Павлович, отчёт готов"])   # jsonb → list
    assert run.roles_found == 1 and run.cost_usd == pytest.approx(0.01)
    card = (await connections.entity_connections(w["owner"]))[0]
    assert card["main"]["source"] == "history" and card["main"]["quotes"][0] == QUOTE
    with _llm() as llm:
        same = await infer_pair(w["owner"], w["boss"])
    assert llm.await_count == 0 and same.skipped == "пакет улик не изменился"

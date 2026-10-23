"""Ворота новой аренды, recover/start и закрытие вопроса на живом Postgres (PR-B1).

На SQLite нет `FOR UPDATE SKIP LOCKED` и настоящих блокировок строк: гонки и SQL-фильтр
`next` по незакрытым вопросам проверяются здесь. Локально:
docker run -p 5433:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
import pytest_asyncio
from vera_shared.timeutil import utc_naive_now

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")

pytestmark = [
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                       reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]
R = "main"
PROOF = {"stop_source": "process_absent_observed", "effects": "none_known",
         "residual_risk_ack": True, "run_ref": "run-1"}
PICK = {"room": R, "project": None, "lease_seconds": 60, "session": None, "account": None}


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import models, models_room  # noqa: F401
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


async def expire(get_session, task_id: str) -> None:
    from vera_shared.db.models_room import RoomTaskRow
    async with get_session() as s:
        (await s.get(RoomTaskRow, (R, task_id))).lease_until = \
            utc_naive_now() - timedelta(seconds=5)


async def test_next_sql_filter_skips_open_answered_and_recovery_hold(pg_db):
    from vera_shared.db.models_room import RoomTaskRow
    from vera_shared.room import questions, task_queue, tasks

    get_session = pg_db
    for tid in ("open-q", "answered-q", "held", "free"):
        await tasks.open_task(room=R, task_id=tid, agent="claude", title=tid, paths=None,
                              auto_pickup=True, priority=1 if tid != "free" else 3)
    for tid in ("open-q", "answered-q"):
        tok = (await tasks.claim(room=R, task_id=tid, agent="claude", lease_seconds=600)
               )["fencing_token"]
        qid = (await questions.ask(room=R, task_id=tid, agent="claude", fencing_token=tok,
                                   question="?"))["qid"]
        if tid == "answered-q":
            await questions.answer(room=R, task_id=tid, qid=qid, text="да")
        async with get_session() as s:  # легаси-правка: status=open, вопрос остался
            row = await s.get(RoomTaskRow, (R, tid))
            row.status, row.lease_holder, row.lease_until = "open", None, None
            row.auto_pickup = True
    async with get_session() as s:
        (await s.get(RoomTaskRow, (R, "held"))).recovery_hold = True
    got = await task_queue.next_task(agent="codex", **PICK)
    assert got["task_id"] == "free"
    assert await task_queue.next_task(agent="codex", **PICK) is None


async def test_concurrent_claims_with_gate_give_exactly_one_lease(pg_db):
    from vera_shared.room import tasks

    await tasks.open_task(room=R, task_id="race", agent="claude", title="r", paths=None)
    results = await asyncio.gather(*[
        tasks.claim(room=R, task_id="race", agent=f"a{i}", lease_seconds=600)
        for i in range(6)], return_exceptions=True)
    won = [r for r in results if isinstance(r, dict)]
    assert len(won) == 1 and all(isinstance(r, tasks.TaskBusy) for r in results if r not in won)
    assert won[0]["fencing_token"] == 1


async def test_recover_then_claim_race_never_issues_a_lease_before_start(pg_db):
    from vera_shared.db.models_room import RoomTaskRow
    from vera_shared.room import recovery, tasks

    get_session = pg_db
    tok = (await tasks.claim(room=R, task_id="rc", agent="claude", lease_seconds=600)
           )["fencing_token"]
    await expire(get_session, "rc")
    recover = recovery.recover(room=R, task_id="rc", expected_fencing_token=tok, **PROOF)
    claims = [tasks.claim(room=R, task_id="rc", agent=f"a{i}", lease_seconds=600)
              for i in range(4)]
    out = await asyncio.gather(recover, *claims, return_exceptions=True)
    leases = [r for r in out[1:] if isinstance(r, dict)]
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (R, "rc"))
        if isinstance(out[0], dict):  # recover выиграл блокировку: ни одной аренды до start
            assert not leases and row.recovery_hold is True and row.lease_holder is None
            assert all(isinstance(r, tasks.TaskBusy) for r in out[1:])
        else:  # claim выиграл раньше: ровно одна живая аренда, recover отказан
            assert len(leases) == 1 and row.recovery_hold is False
            assert isinstance(out[0], tasks.StaleLease | recovery.RecoverRefused)


async def test_recover_start_claim_ack_flow_on_postgres(pg_db):
    from vera_shared.db.models_room import RoomTaskRow
    from vera_shared.room import questions, recovery, tasks

    get_session = pg_db
    tok = (await tasks.claim(room=R, task_id="flow", agent="claude", lease_seconds=600)
           )["fencing_token"]
    qid = (await questions.ask(room=R, task_id="flow", agent="claude", fencing_token=tok,
                               question="?"))["qid"]
    await questions.answer(room=R, task_id="flow", qid=qid, text="да")
    await expire(get_session, "flow")
    await recovery.recover(room=R, task_id="flow", expected_fencing_token=tok, **PROOF)
    with pytest.raises(tasks.TaskBusy, match="recovery_hold"):
        await tasks.claim(room=R, task_id="flow", agent="codex", lease_seconds=60)
    await recovery.start(room=R, task_id="flow", expected_fencing_token=tok + 1)
    got = await tasks.claim(room=R, task_id="flow", agent="codex", lease_seconds=60)
    assert got["answered_questions"][0]["qid"] == qid
    await questions.ack(room=R, task_id="flow", qid=qid, agent="codex",
                        fencing_token=got["fencing_token"])
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (R, "flow"))
        assert (row.status, row.recovery_hold) == ("in_progress", False)

"""Office intake на живом Postgres: гонка двух ответов и гонка UNIQUE(principal, client_task_id).

SQLite не воспроизводит `SELECT … FOR UPDATE` и реальную конкуренцию транзакций, поэтому
эти гарантии проверяются здесь. Комната `office-pg`, не `main`.
"""
from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")
ROOM = "office-pg"

pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get("RUN_INTEGRATION_TESTS") or os.environ.get("TEST_DATABASE_URL")),
        reason="Set TEST_DATABASE_URL (or RUN_INTEGRATION_TESTS=1)"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("OFFICE_ROOM", ROOM)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


def _principal():
    from vera_shared.office.acl import OfficePrincipal

    return OfficePrincipal(name="vbot", project="Veranda")


async def _create(cid: str = "tg--100-1"):
    from vera_shared.office import intake

    return await intake.create_task(
        _principal(), cid, "Вода", "Заказать воду", {"channel": "telegram", "chat": "-100"},
        {"channel": "telegram", "chat": "-100"})


@pytest.mark.usefixtures("pg_db")
async def test_concurrent_create_same_client_task_id_makes_one_task():
    from sqlalchemy import func, select
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_office import RoomTaskOriginRow
    from vera_shared.db.models_room import RoomTaskRow

    results = await asyncio.gather(*[_create() for _ in range(6)], return_exceptions=True)
    assert not [r for r in results if isinstance(r, Exception)], results
    assert len({r["task_id"] for r in results}) == 1
    assert sum(1 for r in results if r["created"]) == 1
    async with get_session() as s:
        origins = (await s.execute(select(func.count()).select_from(RoomTaskOriginRow))).scalar()
        rooms = (await s.execute(select(func.count()).select_from(RoomTaskRow))).scalar()
    assert (origins, rooms) == (1, 1)


@pytest.mark.usefixtures("pg_db")
async def test_concurrent_answers_with_different_keys_second_gets_409():
    from sqlalchemy import func, select
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_room import RoomTaskAnswerRow
    from vera_shared.office import intake, repository
    from vera_shared.room import questions, tasks

    made = await _create()
    held = await tasks.claim(room=ROOM, task_id=made["task_id"], agent="claude",
                             lease_seconds=300)
    q = await questions.ask(room=ROOM, task_id=made["task_id"], agent="claude",
                            fencing_token=held["fencing_token"], question="Сколько?")
    async with get_session() as s:
        rev = await repository.revision_of(s, ROOM, made["task_id"])

    async def go(key: str, text: str):
        return await intake.answer(_principal(), made["task_id"], q["qid"], text, rev, key)

    results = await asyncio.gather(go("k1", "Две"), go("k2", "Три"), return_exceptions=True)
    ok = [r for r in results if isinstance(r, dict)]
    failed = [r for r in results if isinstance(r, intake.IntakeConflict)]
    assert len(ok) == 1 and len(failed) == 1, results
    assert failed[0].code == "revision_mismatch"
    async with get_session() as s:
        answers = (await s.execute(select(func.count()).select_from(RoomTaskAnswerRow))).scalar()
    assert answers == 1


@pytest.mark.usefixtures("pg_db")
async def test_answer_commits_atomically_or_not_at_all(monkeypatch):
    from sqlalchemy import func, select
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_office import OfficeAnswerKeyRow
    from vera_shared.db.models_room import (
        RoomMessageRow,
        RoomTaskAnswerRow,
        RoomTaskEventRow,
        RoomTaskQuestionRow,
    )
    from vera_shared.office import intake, repository
    from vera_shared.room import questions, tasks

    made = await _create()
    held = await tasks.claim(room=ROOM, task_id=made["task_id"], agent="claude",
                             lease_seconds=300)
    q = await questions.ask(room=ROOM, task_id=made["task_id"], agent="claude",
                            fencing_token=held["fencing_token"], question="Сколько?")

    async def state() -> tuple[int, ...]:
        async with get_session() as s:
            async def n(model, *where):
                return await s.scalar(select(func.count()).select_from(model).where(*where))
            return (await n(RoomTaskAnswerRow), await n(OfficeAnswerKeyRow),
                    await n(RoomTaskEventRow, RoomTaskEventRow.kind == "answered"),
                    await n(RoomMessageRow, RoomMessageRow.message_id.like("task-a-%")),
                    await n(RoomTaskQuestionRow, RoomTaskQuestionRow.status == "open"))

    async with get_session() as s:
        rev = await repository.revision_of(s, ROOM, made["task_id"])
    assert await state() == (0, 0, 0, 0, 1)
    real = repository.insert_answer_key

    async def boom(*_a, **_k):
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(repository, "insert_answer_key", boom)
    with pytest.raises(RuntimeError, match="crash"):
        await intake.answer(_principal(), made["task_id"], q["qid"], "Две", rev, "k")
    assert await state() == (0, 0, 0, 0, 1)
    monkeypatch.setattr(repository, "insert_answer_key", real)
    done = await intake.answer(_principal(), made["task_id"], q["qid"], "Две", rev, "k")
    again = await intake.answer(_principal(), made["task_id"], q["qid"], "Две", rev, "k")
    assert not done["replayed"] and again["replayed"] and again["revision"] == done["revision"]
    assert await state() == (1, 1, 1, 1, 0)

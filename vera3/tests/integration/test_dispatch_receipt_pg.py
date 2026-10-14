"""Диспетчер офиса на живом Postgres: квитанция, гонка двух проходов, очередь >50.

SQLite не воспроизводит `FOR UPDATE SKIP LOCKED` и приведение JSON к тексту, поэтому
эти гарантии проверяются здесь. Комната `voice-test`, не `main`.
"""
from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")
ROOM = "voice-test"

pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get("RUN_INTEGRATION_TESTS") or os.environ.get("TEST_DATABASE_URL")),
        reason="Set TEST_DATABASE_URL (or RUN_INTEGRATION_TESTS=1)"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


async def _open(task_id: str, refs: list[dict] | None = None) -> None:
    from vera_shared.room import tasks

    await tasks.open_task(room=ROOM, task_id=task_id, agent="vera", title="починить бота",
                          paths=None, project="Vera", priority=1, auto_pickup=True, refs=refs)


async def _dispatch_ids() -> list[str]:
    from vera_shared.room.messages import history

    return [m["message_id"] for m in await history(room=ROOM, limit=500)
            if m["message_id"].startswith("dispatch-")]


async def _row(task_id: str) -> dict:
    from vera_shared.room.task_queue import list_tasks

    return next(t for t in await list_tasks(room=ROOM, status=None, limit=500)
                if t["task_id"] == task_id)


@pytest.mark.usefixtures("pg_db")
async def test_receipt_releases_open_and_no_ack_keeps_waiting():
    from bot_telegram.dispatcher import dispatch_one
    from vera_shared.room.messages import post_message

    await _open("T1")
    assert await dispatch_one() is True
    row = await _row("T1")
    assert row["status"] == "in_progress" and row["lease_holder"] == "vera"
    assert row["waiting_until"] is not None
    assert await dispatch_one() is False  # квитанции нет — задача не отпущена

    await post_message(room=ROOM, message_id="ack-T1", from_agent="dot", to_agent="vera",
                       task_id="T1", status="ack", in_reply_to="dispatch-T1", body="ок")
    assert await dispatch_one() is True
    row = await _row("T1")
    assert row["status"] == "open" and row["lease_holder"] is None
    assert await _dispatch_ids() == ["dispatch-T1"]


@pytest.mark.usefixtures("pg_db")
async def test_concurrent_dispatch_one_exactly_one_post_and_fencing():
    from bot_telegram.dispatcher import dispatch_one

    await _open("T1")
    results = await asyncio.gather(dispatch_one(), dispatch_one(), return_exceptions=True)
    assert not [r for r in results if isinstance(r, Exception)], results
    assert sorted(results) == [False, True]
    assert await _dispatch_ids() == ["dispatch-T1"]
    row = await _row("T1")
    assert row["lease_holder"] == "vera" and row["fencing_token"] == 1


@pytest.mark.usefixtures("pg_db")
async def test_concurrent_claim_of_same_task_one_winner():
    from vera_shared.room.tasks import TaskBusy, claim

    await _open("T1")
    results = await asyncio.gather(
        *(claim(room=ROOM, task_id="T1", agent=a, lease_seconds=60)
          for a in ("vera", "codex")), return_exceptions=True)
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1 and winners[0]["fencing_token"] == 1
    assert all(isinstance(r, TaskBusy) for r in results if not isinstance(r, dict))


@pytest.mark.usefixtures("pg_db")
async def test_queue_over_candidates_limit_new_task_still_dispatched():
    from bot_telegram.dispatcher import dispatch_one

    for i in range(55):
        await _open(f"D{i:02d}", [{"kind": "source", "ref": "dispatched: dispatch-x"}])
    await _open("NEW")
    assert await dispatch_one() is True
    assert await _dispatch_ids() == ["dispatch-NEW"]


@pytest.mark.usefixtures("pg_db")
async def test_owner_office_route_requires_creator_and_exact_marker():
    from bot_telegram.dispatcher import dispatch_one
    from vera_shared.room import tasks

    for task_id, creator, marker in (
        ("foreign", "vera", "owner_dispatch: dot"),
        ("near", "veranda_bot", "owner_dispatch: dot-extra"),
        ("owner", "veranda_bot", "owner_dispatch: dot"),
    ):
        await tasks.open_task(
            room=ROOM, task_id=task_id, agent=creator, title="owner request", paths=None,
            project="Veranda", auto_pickup=True,
            refs=[{"kind": "source", "ref": marker}])
    assert await dispatch_one() is True
    assert await _dispatch_ids() == ["dispatch-owner"]

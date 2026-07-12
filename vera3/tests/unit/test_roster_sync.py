"""ingestor_telegram.roster_sync — лурκеры проектных групп в граф.

Telethon-клиент мокается; project_membership/entities/memberships — реальная
SQLite (та же фикстура, что в test_identity)."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test")
os.environ.setdefault("TELEGRAM_PHONE", "+10000000000")

from sqlalchemy import text  # noqa: E402
from vera_shared.db import (  # noqa: E402
    models,  # noqa: F401
    models_graph,  # noqa: F401
)
from vera_shared.db.engine import Base, get_session  # noqa: E402


@pytest_asyncio.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'roster.db'}"
    import vera_shared.db.engine as engine_mod
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None
    from vera_shared.db.engine import init_engine
    engine = await init_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None


def _tg_user(uid, first, username=None, bot=False, deleted=False):
    return SimpleNamespace(id=uid, first_name=first, last_name=None,
                           username=username, bot=bot, deleted=deleted)


async def _seed_project_chat(tg_id=555, project="itstep"):
    """group-сущность + alias + строка project_membership."""
    from vera_shared.graph import repo
    chat_eid = await repo.upsert_entity(
        type="group", name="J Branch Internal", source="telegram",
        identifier=f"chat:{tg_id}", attributes={"tg_id": tg_id})
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO project_membership (project, kind, key, label, source) "
            "VALUES (:p, 'chat', :k, 'test', 'test')"
        ), {"p": project, "k": str(tg_id)})
    return chat_eid


@pytest.mark.asyncio
async def test_project_chats_resolves_via_alias(db):
    from ingestor_telegram.roster_sync import project_chats
    chat_eid = await _seed_project_chat()
    chats = await project_chats()
    assert len(chats) == 1
    assert chats[0]["entity_id"] == chat_eid
    assert chats[0]["type"] == "group"
    assert str(chats[0]["tg_id"]) == "555"


@pytest.mark.asyncio
async def test_sync_chat_roster_adds_lurkers_skips_bots(db):
    from ingestor_telegram.roster_sync import project_chats, sync_chat_roster
    await _seed_project_chat()
    chat = (await project_chats())[0]

    client = SimpleNamespace(get_participants=AsyncMock(return_value=[
        _tg_user(1, "Дарья", "daria"),
        _tg_user(2, "Молчун Лурκер"),          # никогда не писал
        _tg_user(3, "Bot", "somebot", bot=True),
        _tg_user(4, "Del", deleted=True),
    ]))
    n = await sync_chat_roster(client, chat)
    assert n == 2   # боты и удалённые скипнуты

    async with get_session() as s:
        members = (await s.execute(text(
            "SELECT count(*) FROM memberships WHERE parent_entity_id=:c"
        ), {"c": chat["entity_id"]})).scalar_one()
        lurker = (await s.execute(text(
            "SELECT count(*) FROM entities WHERE name='Молчун Лурκер'"
        ))).scalar_one()
    assert members == 2 and lurker == 1


@pytest.mark.asyncio
async def test_run_roster_sync_survives_chat_failure(db):
    from ingestor_telegram import roster_sync
    from telethon.errors import ChatAdminRequiredError
    await _seed_project_chat(tg_id=555)
    await _seed_project_chat(tg_id=777)

    calls = {"n": 0}

    async def flaky(peer, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChatAdminRequiredError(None)
        return [_tg_user(9, "Ok")]

    client = SimpleNamespace(get_participants=AsyncMock(side_effect=flaky))
    with patch.object(roster_sync, "CHAT_DELAY_S", 0):
        stats = await roster_sync.run_roster_sync(client)
    assert stats["chats"] == 1 and stats["people"] == 1
    assert len(stats["skipped"]) == 1
    assert roster_sync.state["running"] is False

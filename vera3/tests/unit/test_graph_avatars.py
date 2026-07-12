"""vera_shared.graph.avatars + dedup.find_alias_collisions / get_entity_context
against a real in-memory SQLite (same fixture pattern as test_graph_repo.py) —
covers the actual SQL (JSON `->>`, LEFT JOIN, upsert branches)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from vera_shared.db import (
    models,  # noqa: F401  — registers events table on Base
    models_graph,  # noqa: F401  — registers graph tables
)
from vera_shared.db.engine import Base


@pytest_asyncio.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'av.db'}"
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


async def _seed_person(name, ident, username, tg_id):
    from vera_shared.graph import repo
    return await repo.upsert_entity(
        type="person", name=name, source="telegram", identifier=ident,
        attributes={"username": username, "tg_id": tg_id},
    )


@pytest.mark.asyncio
async def test_avatar_upsert_get_and_missing(db):
    from vera_shared.graph.avatars import get_avatar, upsert_avatar
    eid = await _seed_person("Дима", "user:1", "dimaod", 1)

    assert await get_avatar(eid) is None          # nothing stored yet

    await upsert_avatar(eid, image=b"jpegdata", mime="image/jpeg")
    got = await get_avatar(eid)
    assert got == (b"jpegdata", "image/jpeg")

    # update path → mark missing, image cleared
    await upsert_avatar(eid, image=None, missing=True)
    assert await get_avatar(eid) is None


@pytest.mark.asyncio
async def test_list_entities_needing_avatar_excludes_fetched(db):
    from vera_shared.graph.avatars import (
        list_entities_needing_avatar,
        upsert_avatar,
    )
    e1 = await _seed_person("A", "user:1", "a_u", 1)
    e2 = await _seed_person("B", "user:2", "b_u", 2)

    ids = {r["id"] for r in await list_entities_needing_avatar(limit=10)}
    assert {e1, e2} <= ids

    await upsert_avatar(e1, image=b"x")
    ids2 = {r["id"] for r in await list_entities_needing_avatar(limit=10)}
    assert e1 not in ids2 and e2 in ids2


@pytest.mark.asyncio
async def test_list_needing_avatar_ids_filter(db):
    from vera_shared.graph.avatars import list_entities_needing_avatar
    e1 = await _seed_person("A", "user:1", "a_u", 1)
    await _seed_person("B", "user:2", "b_u", 2)
    ids = {r["id"] for r in await list_entities_needing_avatar(limit=10, ids=[e1])}
    assert ids == {e1}


@pytest.mark.asyncio
async def test_find_alias_collisions_real_db(db):
    from vera_shared.graph import repo
    from vera_shared.graph.dedup import find_alias_collisions
    # same @username as both a channel and a person → real duplicate
    await repo.upsert_entity(type="channel", name="News", source="telegram",
                             identifier="chat:-100", attributes={"username": "newschan"})
    await repo.upsert_entity(type="person", name="News", source="telegram",
                             identifier="user:5", attributes={"username": "NewsChan"})
    await _seed_person("Solo", "user:9", "unique_one", 9)

    groups = await find_alias_collisions(min_group=2)
    assert len(groups) == 1
    assert groups[0]["username"] == "newschan"
    assert groups[0]["size"] == 2


@pytest.mark.asyncio
async def test_get_entity_context_returns_identity_fields(db):
    from vera_shared.graph.dedup import get_entity_context
    eid = await _seed_person("Дима Груздев", "user:1", "devgruz", 424690620)
    ctx = await get_entity_context(eid)
    assert ctx["name"] == "Дима Груздев"
    assert ctx["type"] == "person"
    assert ctx["username"] == "devgruz"
    assert str(ctx["tg_id"]) == "424690620"


def test_msg_snippet_strips_header():
    from vera_shared.graph.dedup import _msg_snippet
    raw = ("Author: @x [counterparty]\nFrom: X\nChat: Y\nDate: 2026\n"
           "Direction: received\n---\nПривет,   как   дела?")
    assert _msg_snippet(raw) == "Привет, как дела?"
    assert _msg_snippet(None) == ""
    assert _msg_snippet("no header text") == "no header text"


@pytest.mark.asyncio
async def test_get_entity_dossier_samples_chats_project(db):
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow
    from vera_shared.graph.dedup import get_entity_dossier
    eid = await _seed_person("Маша", "user:7", "masha", 777)

    async with get_session() as s:
        for i, (chat, proj) in enumerate([("Семья", "family"), ("Семья", "family"),
                                          ("IT STEP", "itstep")]):
            s.add(EventRow(
                source="telegram", source_event_id=f"m{i}",
                content_text=f"Author: Маша\n---\nсообщение {i}",
                occurred_at=__import__("datetime").datetime(2026, 7, i + 1),
                triage_status="done", project=proj,
                metadata_={"sender_id": "777", "chat_title": chat},
            ))

    d = await get_entity_dossier(eid)
    assert d["name"] == "Маша"
    assert d["msg_count"] == 3
    assert d["dom_project"] == "family"          # 2 family vs 1 itstep
    assert ("Семья", 2) in d["top_chats"]
    assert len(d["samples"]) == 3
    assert all("Author:" not in s for s in d["samples"])   # header stripped

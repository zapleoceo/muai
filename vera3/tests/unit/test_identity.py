"""vera_shared.graph.identity — name canonicalisation, candidate pairs,
suggestion storage (real SQLite, same fixture pattern as test_graph_avatars),
plus clusters.label_propagation (pure) and the strict namesake resolve."""
from __future__ import annotations

import pytest
import pytest_asyncio
from vera_shared.db import (
    models,  # noqa: F401  — registers events table on Base
    models_graph,  # noqa: F401  — registers graph tables (incl. merge_suggestions)
)
from vera_shared.db.engine import Base
from vera_shared.graph.identity import canonical_name_parts

# ─── canonical_name_parts ───────────────────────────────────────────────────


def test_diminutive_and_translit_fold_to_same_first():
    assert canonical_name_parts("Маша")[0] == "мария"
    assert canonical_name_parts("Мария Иванова")[0] == "мария"
    assert canonical_name_parts("Masha")[0] == "мария"
    assert canonical_name_parts("Оля")[0] == "ольга"
    assert canonical_name_parts("Ольга Олеговая")[0] == "ольга"
    assert canonical_name_parts("Olga")[0] == "ольга"
    assert canonical_name_parts("Дима 🏝️")[0] == "дмитрий"
    assert canonical_name_parts("Dima Zaporozhets")[0] == "дмитрий"


def test_last_name_translit_fold():
    _, last_cyr = canonical_name_parts("Мария Иванова")
    _, last_lat = canonical_name_parts("Maria Ivanova")
    assert last_cyr == last_lat == "иванова"


def test_empty_and_garbage_names():
    assert canonical_name_parts(None) == ("", "")
    assert canonical_name_parts("🏝️💥") == ("", "")
    assert canonical_name_parts("  ") == ("", "")


# ─── DB-backed: candidates + suggestions ────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'id.db'}"
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


async def _person(name, ident, source="telegram", **attrs):
    from vera_shared.graph import repo
    return await repo.upsert_entity(
        type="person", name=name, source=source, identifier=ident,
        attributes=attrs or {},
    )


@pytest.mark.asyncio
async def test_candidates_cross_source_pair_found(db):
    from vera_shared.graph.identity import find_identity_candidates
    a = await _person("Маша", "user:1", source="telegram", tg_id=1)
    b = await _person("Maria Ivanova", "maria@x.com", source="gmail")
    await _person("Пётр", "user:9", source="telegram", tg_id=9)   # not a match

    pairs = await find_identity_candidates(limit=10)
    assert (min(a, b), max(a, b), "разные каналы (кросс-источник)") in pairs


@pytest.mark.asyncio
async def test_candidates_same_source_needs_shared_chat_or_lastname(db):
    from vera_shared.graph import repo
    from vera_shared.graph.identity import find_identity_candidates
    a = await _person("Оля", "user:1", tg_id=1)
    b = await _person("Ольга Олеговая", "user:2", tg_id=2)
    # same source, no shared chat, no matching last name → NOT a candidate
    assert await find_identity_candidates(limit=10) == []

    chat = await repo.upsert_entity(type="group", name="Чат",
                                    source="telegram", identifier="chat:-1")
    await repo.upsert_membership(parent_entity_id=chat, child_entity_id=a,
                                 source="telegram")
    await repo.upsert_membership(parent_entity_id=chat, child_entity_id=b,
                                 source="telegram")
    pairs = await find_identity_candidates(limit=10)
    assert (min(a, b), max(a, b), "общие чаты") in pairs


@pytest.mark.asyncio
async def test_judged_pairs_never_reasked_and_status_flow(db):
    from vera_shared.graph.identity import (
        find_identity_candidates,
        list_pending_suggestions,
        save_suggestion,
        set_suggestion_status,
    )
    a = await _person("Маша", "user:1", source="telegram", tg_id=1)
    b = await _person("Masha", "m@x.com", source="gmail")

    await save_suggestion(a, b, {"verdict": "same", "confidence": 0.9,
                                 "reason": "одинаковый стиль"})
    assert await find_identity_candidates(limit=10) == []   # judged → excluded

    pending = await list_pending_suggestions()
    assert len(pending) == 1 and pending[0]["verdict"] == "same"

    row = await set_suggestion_status(pending[0]["id"], "accepted")
    assert {row["entity_a"], row["entity_b"]} == {a, b}
    assert await list_pending_suggestions() == []


@pytest.mark.asyncio
async def test_different_verdict_stored_pre_rejected(db):
    from vera_shared.graph.identity import (
        list_pending_suggestions,
        save_suggestion,
    )
    a = await _person("Дима", "user:1", tg_id=1)
    b = await _person("Дима", "d@x.com", source="gmail")
    await save_suggestion(a, b, {"verdict": "different", "confidence": 0.8,
                                 "reason": "разные темы"})
    assert await list_pending_suggestions() == []   # never shown, never re-asked


# ─── strict namesake resolve (rel_extract data-quality fix) ─────────────────


@pytest.mark.asyncio
async def test_resolve_entity_exact_refuses_ambiguous_namesakes(db):
    # ASCII on purpose: SQLite lower() folds ASCII only (same caveat as
    # test_graph_repo). Postgres LOWER() handles Cyrillic in prod.
    from vera_shared.graph import repo
    only = await _person("Unicum", "user:1", tg_id=1)
    assert await repo.resolve_entity_exact("unicum") == only

    await _person("Dima", "user:2", tg_id=2)
    await _person("Dima", "user:3", tg_id=3)
    assert await repo.resolve_entity_exact("Dima") is None   # ambiguous → skip


# ─── clusters.label_propagation (pure) ──────────────────────────────────────


def test_label_propagation_two_communities():
    from vera_shared.graph.clusters import label_propagation
    # two triangles bridged by one weak edge
    nodes = [1, 2, 3, 10, 11, 12]
    edges = [(1, 2), (2, 3), (1, 3), (10, 11), (11, 12), (10, 12), (3, 10)]
    assign = label_propagation(nodes, edges)
    assert assign[1] == assign[2] == assign[3]
    assert assign[10] == assign[11] == assign[12]
    # communities renumbered from 0 by size
    assert set(assign.values()) <= {0, 1}


def test_label_propagation_deterministic():
    from vera_shared.graph.clusters import label_propagation
    nodes = list(range(30))
    edges = [(i, (i + 1) % 15) for i in range(15)] + \
            [(15 + i, 15 + (i + 1) % 15) for i in range(15)]
    a = label_propagation(nodes, edges)
    b = label_propagation(nodes, edges)
    assert a == b


# ─── LLM paths (chat mocked at the broker-client boundary) ──────────────────


@pytest.mark.asyncio
async def test_judge_pair_parses_verdict(db):
    import json
    from unittest.mock import AsyncMock, patch

    from vera_shared.graph.identity import judge_pair
    a = await _person("Маша", "user:1", tg_id=1)
    b = await _person("Masha", "m@x.com", source="gmail")
    reply = json.dumps({"verdict": "same", "confidence": 0.87,
                        "reason": "стиль и чаты совпадают"})
    with patch("vera_shared.llm.client.chat_async",
               AsyncMock(return_value=(reply, {"provider": "t"}))):
        v = await judge_pair(a, b, "разные каналы (кросс-источник)")
    assert v == {"verdict": "same", "confidence": 0.87,
                 "reason": "стиль и чаты совпадают"}


@pytest.mark.asyncio
async def test_judge_pair_rejects_bad_verdict(db):
    from unittest.mock import AsyncMock, patch

    from vera_shared.graph.identity import judge_pair
    a = await _person("Оля", "user:1", tg_id=1)
    b = await _person("Olga", "o@x.com", source="gmail")
    with patch("vera_shared.llm.client.chat_async",
               AsyncMock(return_value=('{"verdict":"maybe"}', {}))), \
         pytest.raises(ValueError):
        await judge_pair(a, b, "сигнал")


@pytest.mark.asyncio
async def test_run_identity_analysis_stats_and_resilience(db):
    from unittest.mock import patch

    from vera_shared.graph import identity
    from vera_shared.graph.identity import (
        list_pending_suggestions,
        run_identity_analysis,
    )
    await _person("Маша", "user:1", tg_id=1)
    await _person("Masha", "m@x.com", source="gmail")
    await _person("Оля", "user:2", tg_id=2)
    await _person("Olga", "o@x.com", source="gmail")

    verdicts = [
        {"verdict": "same", "confidence": 0.9, "reason": "улики"},
        RuntimeError("broker down"),          # second pair fails → skipped
    ]

    async def fake_judge(a, b, signal):
        v = verdicts.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    with patch.object(identity, "judge_pair", side_effect=fake_judge):
        stats = await run_identity_analysis(max_pairs=5)
    assert stats["judged"] == 1 and stats["same"] == 1 and stats["failed"] == 1
    assert len(await list_pending_suggestions()) == 1


@pytest.mark.asyncio
async def test_name_clusters_llm_labels_and_fallback(db):
    from unittest.mock import AsyncMock, patch

    from vera_shared.graph.clusters import MIN_LABELED_SIZE, name_clusters_llm
    nodes = [{"id": i, "name": f"N{i}", "type": "person", "degree": 1}
             for i in range(MIN_LABELED_SIZE * 2)]
    assign = {n["id"]: (0 if n["id"] < MIN_LABELED_SIZE else 1) for n in nodes}

    calls = [('{"label": "Команда IT STEP"}', {}), RuntimeError("boom")]

    async def fake_chat(**kwargs):
        v = calls.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    with patch("vera_shared.llm.client.chat_async", AsyncMock(side_effect=fake_chat)):
        labels = await name_clusters_llm(assign, nodes)
    assert labels[0] == "Команда IT STEP"
    assert labels[1] == "кластер 2"          # broker failure → fallback


@pytest.mark.asyncio
async def test_recompute_and_get_clusters_roundtrip(db):
    # set_control/get_control are Postgres-only glue (raw now() SQL) — fake
    # the KV in-memory; the graph side (snapshot → communities) runs for real.
    from unittest.mock import AsyncMock, patch

    from vera_shared.graph import clusters as cl
    from vera_shared.graph import repo
    a = await _person("A", "user:1", tg_id=1)
    b = await _person("B", "user:2", tg_id=2)
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="friend_of", confidence=0.9)

    kv: dict[str, str] = {}

    async def fake_set(key, value):
        kv[key] = value

    async def fake_get(key, default=""):
        return kv.get(key, default)

    with patch.object(cl, "set_control", fake_set), \
         patch.object(cl, "get_control", fake_get), \
         patch("vera_shared.llm.client.chat_async",
               AsyncMock(return_value=('{"label": "Пара"}', {}))):
        payload = await cl.recompute_clusters(limit=50)
        cached = await cl.get_clusters()
        assert cached is not None and cached["assign"] == payload["assign"]
    assert payload["nodes"] == 2 and str(a) in payload["assign"]


# ─── rel_extract: «я» → автор события, не сущность с именем «Я» ─────────────


@pytest.mark.asyncio
async def test_author_entity_of_event_maps_self_to_author(db):
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow
    from vera_shared.graph.rel_extract import author_entity_of_event
    from vera_shared.projects.rules import OWNER_TG_ID

    owner = await _person("Dima Z", f"user:{OWNER_TG_ID}", tg_id=OWNER_TG_ID)
    sender = await _person("Vasya", "user:555", tg_id=555)

    async with get_session() as s:
        s.add(EventRow(id=1, source="telegram", source_event_id="e1",
                       content_text="x", triage_status="done",
                       occurred_at=__import__("datetime").datetime(2026, 7, 1),
                       metadata_={"direction": "received", "sender_id": 555}))
        s.add(EventRow(id=2, source="telegram", source_event_id="e2",
                       content_text="x", triage_status="done",
                       occurred_at=__import__("datetime").datetime(2026, 7, 1),
                       metadata_={"direction": "sent",
                                  "sender_id": OWNER_TG_ID}))
        s.add(EventRow(id=3, source="gmail", source_event_id="e3",
                       content_text="x", triage_status="done",
                       occurred_at=__import__("datetime").datetime(2026, 7, 1),
                       metadata_={"direction": "received",
                                  "from": "Vasya <vasya@corp.com>"}))

    assert await author_entity_of_event(1) == sender      # received → отправитель
    assert await author_entity_of_event(2) == owner       # sent → владелец
    assert await author_entity_of_event(999) is None      # нет события

    from vera_shared.graph import repo
    gmail_guy = await repo.upsert_entity(
        type="person", name="Vasya", source="gmail",
        identifier="vasya@corp.com", attributes={"email": "vasya@corp.com"})
    assert await author_entity_of_event(3) == gmail_guy   # gmail received → From


@pytest.mark.asyncio
async def test_extract_and_store_self_token_goes_to_author(db):
    import json
    from unittest.mock import AsyncMock, patch

    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow
    from vera_shared.graph import repo
    from vera_shared.graph.rel_extract import extract_and_store

    imposter = await _person("Я", "user:333", tg_id=333)      # чужой «Я»
    sender = await _person("Vasya", "user:555", tg_id=555)
    org = await repo.upsert_entity(type="person", name="Acme",
                                   source="telegram", identifier="chat:-9")

    async with get_session() as s:
        s.add(EventRow(id=10, source="telegram", source_event_id="e10",
                       content_text="x", triage_status="done",
                       occurred_at=__import__("datetime").datetime(2026, 7, 1),
                       metadata_={"direction": "received", "sender_id": 555}))

    reply = json.dumps({"relationships": [
        {"subject": "Я", "predicate": "works_at", "object": "Acme",
         "fact": "я работаю в Acme", "confidence": 0.9}]})
    with patch("vera_shared.graph.rel_extract.chat_async",
               AsyncMock(return_value=(reply, {}))):
        n = await extract_and_store(10, "я работаю в Acme уже три года, это моя основная работа")
    assert n == 1

    from sqlalchemy import text as _t
    async with get_session() as s:
        rows = (await s.execute(_t(
            "SELECT subject_entity_id, object_entity_id FROM relationships"
        ))).all()
    assert rows == [(sender, org)]        # НЕ imposter «Я»
    assert imposter != sender

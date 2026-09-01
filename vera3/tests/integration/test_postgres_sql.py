"""Запросы, которые НЕ МОГУТ исполниться на SQLite — и потому не исполнялись
ни одним тестом.

Покрытие честно показывало 0% по телу `dashboard.stats._compute_stats` и по
телу `brain_search.search`: на тестовой SQLite они падают на первой же
конструкции (`FILTER (WHERE …)` ещё пройдёт, а `to_tsvector`, `= ANY(:ids)`
и `NOW()` — нет), поэтому их просто никто не звал. Здесь они выполняются на
том же образе, что в проде.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from vera_shared.timeutil import utc_naive_now

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vera:test@localhost:5433/vera_test",
)

# tests/unit/conftest.py сюда не применяется — свои дефолты окружения.
SECRET = "test-internal-secret"

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)


@pytest_asyncio.fixture
async def pg_db(monkeypatch):
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-integration")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod

    # Импорт ВСЕХ модулей моделей обязателен: Base.metadata знает только
    # про те таблицы, чьи классы уже импортированы. Без models_graph
    # create_all молча не создаёт entities/entity_aliases/memberships.
    from vera_shared.db import models, models_graph, models_sources  # noqa: F401
    from vera_shared.db.engine import Base, close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield get_session
    await close_engine()


# ─── dashboard.stats._compute_stats ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_stats_runs_and_aggregates(pg_db):
    """`COUNT(*) FILTER (WHERE …)` по группам плюс окно по usage_log."""
    from dashboard import stats
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow, UsageLogRow

    now = utc_naive_now()
    async with get_session() as s:
        for i in range(4):
            s.add(EventRow(source="telegram", source_event_id=f"t{i}",
                           content_text="x", occurred_at=now,
                           received_at=now, triage_status="done"))
        s.add(EventRow(source="gmail", source_event_id="g1", content_text="x",
                       occurred_at=now, received_at=now, triage_status="pending"))
        s.add(EventRow(source="gmail", source_event_id="g2", content_text="x",
                       occurred_at=now, received_at=now, triage_status="error"))
        s.add(UsageLogRow(provider="broker", model="m", capability="chat:fast",
                          cost_usd=0.25, workflow="triage", created_at=now))

    got = await stats.get_stats(force=True)

    assert got["total"] == 6
    assert got["done"] == 4
    assert got["pending"] == 1
    assert got["error"] == 1
    assert got["backlog_total"] == 2
    assert dict(got["sources_top"]) == {"telegram": 4, "gmail": 2}
    assert got["cost_today"] == pytest.approx(0.25)
    assert got["triage_1h"] == 1


@pytest.mark.asyncio
async def test_usage_log_window_excludes_old_rows(pg_db):
    """Окно `WHERE created_at >= :month` не должно менять цифры — всё, что
    считается FILTER'ами, внутри него. Строка старше окна в месячную сумму
    не входит и раньше не входила."""
    from dashboard import stats
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import UsageLogRow

    now = utc_naive_now()
    async with get_session() as s:
        s.add(UsageLogRow(provider="b", model="m", capability="c",
                          cost_usd=1.0, workflow="triage", created_at=now))
        s.add(UsageLogRow(provider="b", model="m", capability="c",
                          cost_usd=99.0, workflow="triage",
                          created_at=now - timedelta(days=120)))

    got = await stats.get_stats(force=True)
    assert got["cost_month"] == pytest.approx(1.0), "старая строка попала в месяц"


# ─── brain_search.search ────────────────────────────────────────────────────


def _stub_llm(bs, monkeypatch):
    """Брокера в тесте нет. Шов синтеза живёт в synthesis, а не в app —
    после разбора app.py на модули подменять надо там."""
    from brain_search import self_context, synthesis

    async def _no_embed(_texts):
        raise bs.LLMCallFailed("нет брокера в тесте")

    async def _synth(**_kw):
        return "ответ", {"provider": "test", "cost_usd": 0.0}

    monkeypatch.setattr(bs, "embed", _no_embed)
    monkeypatch.setattr(synthesis, "chat_async", _synth)
    monkeypatch.setenv("INTERNAL_SECRET", SECRET)
    self_context.forget()          # кэш переживает базу теста


@pytest.mark.asyncio
async def test_search_fts_branch_runs_on_russian_config(pg_db, monkeypatch):
    """`to_tsvector('russian') @@ to_tsquery('russian', …)` — на SQLite это
    `no such function`, поэтому FTS-ветка поиска не исполнялась никогда."""
    from brain_search import app as bs
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    now = utc_naive_now()
    async with get_session() as s:
        s.add(EventRow(source="telegram", source_event_id="s1",
                       content_text="Договорились о встрече по проекту в Джакарте",
                       occurred_at=now, received_at=now, triage_status="done"))
        s.add(EventRow(source="telegram", source_event_id="s2",
                       content_text="Прогноз погоды на выходные",
                       occurred_at=now, received_at=now, triage_status="done"))

    _stub_llm(bs, monkeypatch)

    res = await bs.search(bs.SearchQuery(q="встреча по проекту", use_agent=False),
                          x_internal_secret=SECRET)

    found = {r.event_id for r in res.results}
    ids = {}
    async with get_session() as s:
        from sqlalchemy import select
        for r in (await s.execute(select(EventRow))).scalars().all():
            ids[r.source_event_id] = r.id
    assert ids["s1"] in found, "русский стеммер не нашёл прямое совпадение"


@pytest.mark.asyncio
async def test_search_time_window_branch(pg_db, monkeypatch):
    """Темпоральная ветка: события вне окна не попадают в выдачу."""
    from brain_search import app as bs
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    now = utc_naive_now()
    async with get_session() as s:
        s.add(EventRow(source="telegram", source_event_id="today",
                       content_text="сегодняшняя запись про отчёт",
                       occurred_at=now, received_at=now, triage_status="done"))
        s.add(EventRow(source="telegram", source_event_id="ancient",
                       content_text="прошлогодняя запись про отчёт",
                       occurred_at=now - timedelta(days=400),
                       received_at=now, triage_status="done"))

    _stub_llm(bs, monkeypatch)

    res = await bs.search(bs.SearchQuery(q="что было сегодня", use_agent=False),
                          x_internal_secret=SECRET)

    previews = " ".join(r.content_preview for r in res.results)
    assert "прошлогодняя" not in previews


# ─── graph_repo: = ANY / expanding IN и degree ──────────────────────────────


@pytest.mark.asyncio
async def test_graph_snapshot_on_postgres(pg_db):
    """Тот же снапшот, что гоняется на SQLite в юнит-тестах, но на настоящем
    диалекте: expanding bindparam в IN, jsonb `attributes->>`, GROUP BY."""
    from vera_shared.graph import repo

    a = await repo.upsert_entity(type="person", name="A", source="s", identifier="a",
                                 attributes={"username": "aaa", "tg_id": 1})
    b = await repo.upsert_entity(type="org", name="B", source="s", identifier="b")
    c = await repo.upsert_entity(type="person", name="C", source="s", identifier="c")
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="works_at", confidence=0.8)
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=c,
                                   predicate="friend_of", confidence=0.7)
    await repo.upsert_membership(parent_entity_id=b, child_entity_id=c, source="s")

    snap = await repo.graph_snapshot(min_degree=1, limit=100)
    by_id = {n["id"]: n for n in snap["nodes"]}

    assert set(by_id) == {a, b, c}
    assert by_id[a]["degree"] == 2
    assert by_id[b]["degree"] == 2      # ребро + членство (родитель)
    assert by_id[c]["degree"] == 2      # ребро + членство (ребёнок)
    assert by_id[a]["username"] == "aaa"    # attributes->>'username'
    assert by_id[a]["tg_id"] == "1"
    assert len(snap["edges"]) == 3      # 2 связи + membership как member_of


@pytest.mark.asyncio
async def test_find_project_chats_matches_membership_to_graph(pg_db):
    """`'chat:' || pm.key` и `attributes->>'tg_id'` — запрос жил в воркере
    ingestor-telegram и джойнил entity_aliases с entities напрямую."""
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import ProjectMembershipRow
    from vera_shared.graph import repo

    grp = await repo.upsert_entity(type="supergroup", name="ITSTEP HQ",
                                   source="telegram", identifier="chat:-100777",
                                   attributes={"tg_id": -100777})
    await repo.upsert_entity(type="person", name="Кто-то",
                             source="telegram", identifier="user:5",
                             attributes={"tg_id": 5})
    async with get_session() as s:
        s.add(ProjectMembershipRow(kind="chat", key="-100777",
                                   project="itstep", source="folder"))
        # аккаунт того же проекта не должен попасть в выборку чатов
        s.add(ProjectMembershipRow(kind="account", key="mail@x",
                                   project="itstep", source="folder"))

    chats = await repo.find_project_chats()

    assert [c["entity_id"] for c in chats] == [grp]
    assert chats[0]["tg_id"] == "-100777"
    assert chats[0]["type"] == "supergroup"


@pytest.mark.parametrize(("question", "mode"), [
    ("встреча по проекту", "fts"),          # есть слова → FTS
    ("что было вчера", "time"),             # только окно
    ("", "recent"),                         # ни слов, ни окна, ни вектора
])
@pytest.mark.asyncio
async def test_retrieval_picks_the_expected_branch(pg_db, question, mode):
    """Ветка выборки перестала быть неявной: раньше это были шесть похожих
    SELECT'ов подряд, и понять, в какой ты, можно было только сравнив их."""
    from brain_search.app import _ts_query
    from brain_search.query_parse import parse_time_range
    from brain_search.retrieval import fetch_candidates

    ts, acc = _ts_query(question)
    found = await fetch_candidates(
        ts_query=ts, acc_words=acc, time_range=parse_time_range(question),
        project=None, has_vector=False, limit=15,
    )
    assert found.mode == mode


@pytest.mark.asyncio
async def test_vector_branch_requires_embedding_via_inner_join(pg_db):
    """Ветка «есть вектор, нет слов» берёт INNER JOIN намеренно: строка без
    эмбеддинга там бесполезна, ранжировать её нечем. Единственное место, где
    JOIN не LEFT, — раньше это отличие терялось среди шести копий."""
    from brain_search.retrieval import fetch_candidates
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventEmbeddingRow, EventRow

    now = utc_naive_now()
    async with get_session() as s:
        with_emb = EventRow(source="telegram", source_event_id="has",
                            content_text="с вектором", occurred_at=now,
                            received_at=now, triage_status="done")
        s.add(with_emb)
        s.add(EventRow(source="telegram", source_event_id="none",
                       content_text="без вектора", occurred_at=now,
                       received_at=now, triage_status="done"))
        await s.flush()
        s.add(EventEmbeddingRow(event_id=with_emb.id, embedding=[0.1, 0.2]))

    found = await fetch_candidates(ts_query="", acc_words=[], time_range=None,
                                   project=None, has_vector=True, limit=15)

    assert found.mode == "vector"
    assert [r[2] for r in found.rows] == ["has"], "строка без эмбеддинга просочилась"


# ─── pgvector: миграция 030 ─────────────────────────────────────────────────
# Расширение объявлено в VERA.md и в образе базы, но не использовалось: колонка
# была JSONB, ANN-индекса не было, косинус считался циклом на Python в двух
# местах. Тесты проверяют ОБА состояния перехода — до бэкфила и после.


async def _apply_pgvector_migration() -> bool:
    """Накатить 030 на тестовую базу. False — расширения нет в сборке."""
    from sqlalchemy import text as sa_text
    from vera_shared.db.engine import get_session
    from vera_shared.db.vectors import forget_capability

    try:
        async with get_session() as s:
            await s.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await s.execute(sa_text(
                "ALTER TABLE event_embeddings "
                "ADD COLUMN IF NOT EXISTS embedding_vec vector(3)"))
    except Exception:
        return False
    forget_capability()
    return True


@pytest.mark.asyncio
async def test_vector_column_detected_only_after_migration(pg_db):
    from vera_shared.db.vectors import forget_capability, vector_column_available

    forget_capability()
    assert await vector_column_available() is False, "колонки ещё нет"

    if not await _apply_pgvector_migration():
        pytest.skip("расширение vector недоступно в этой сборке Postgres")
    assert await vector_column_available() is True


@pytest.mark.asyncio
async def test_capability_is_cached_per_process(pg_db):
    """Каталог не меняется в рантайме — спрашивать его на каждый запрос это
    тот же класс расточительства, что и кулдаун LLM."""
    from vera_shared.db import vectors
    from vera_shared.db.vectors import forget_capability, vector_column_available

    forget_capability()
    await vector_column_available()

    calls = 0
    real = vectors.get_session

    def counting(*a, **kw):
        nonlocal calls
        calls += 1
        return real(*a, **kw)

    vectors.get_session = counting
    try:
        for _ in range(5):
            await vector_column_available()
    finally:
        vectors.get_session = real
    assert calls == 0, "проверка колонки ходит в БД повторно"


@pytest.mark.asyncio
async def test_nearest_neighbour_by_index_matches_python_cosine(pg_db):
    """Главное свойство миграции: ответ не должен измениться. Оператор <=> —
    косинусное РАССТОЯНИЕ, поэтому сходство = 1 - расстояние; перепутать знак
    здесь легко, и тогда дедуп начнёт считать похожими самые ДАЛЁКИЕ факты."""
    from sqlalchemy import text as sa_text
    from vera_shared.db.engine import get_session
    from vera_shared.db.vectors import as_pg_vector

    if not await _apply_pgvector_migration():
        pytest.skip("расширение vector недоступно в этой сборке Postgres")

    from gateway.claude import _cosine

    query = [1.0, 0.0, 0.0]
    corpus = {
        # 0.9997 — уровень настоящего почти-дубля факта, выше порога 0.92
        "почти то же": [0.999, 0.026, 0.0],
        "перпендикуляр": [0.0, 1.0, 0.0],
        "напротив": [-1.0, 0.0, 0.0],
    }
    from vera_shared.db.models import EventRow

    now = utc_naive_now()
    async with get_session() as s:
        for label, vec in corpus.items():
            ev = EventRow(source="claude", source_event_id=label, category="fact",
                          content_text=label, occurred_at=now, received_at=now,
                          triage_status="done")
            s.add(ev)
            await s.flush()
            await s.execute(sa_text(
                "INSERT INTO event_embeddings (event_id, embedding, embedding_vec)"
                " VALUES (:i, CAST(:j AS jsonb), CAST(:v AS vector))"),
                {"i": ev.id, "j": str(vec), "v": as_pg_vector(vec)})

        row = (await s.execute(sa_text("""
            SELECT e.source_event_id, 1 - (ee.embedding_vec <=> CAST(:q AS vector))
            FROM events e JOIN event_embeddings ee ON ee.event_id = e.id
            ORDER BY ee.embedding_vec <=> CAST(:q AS vector) LIMIT 1
        """), {"q": as_pg_vector(query)})).one()

    assert row[0] == "почти то же", "индекс выбрал не ближайшего"
    # та же величина, что дал бы питоновский перебор — знак не перепутан
    assert row[1] == pytest.approx(_cosine(query, corpus["почти то же"]), abs=1e-6)
    # и она проходит порог дедупа, ради которого всё это и считается
    from gateway.claude import SEMANTIC_DEDUP_THRESHOLD
    assert row[1] >= SEMANTIC_DEDUP_THRESHOLD


@pytest.mark.asyncio
async def test_backfill_is_idempotent_and_skips_broken_rows(pg_db):
    """Скрипт можно прервать и запустить снова: условие `embedding_vec IS NULL`
    само сужается. Битая строка удаляется, иначе цикл не закончился бы никогда."""
    from sqlalchemy import text as sa_text
    from vera_shared.db.engine import get_session

    if not await _apply_pgvector_migration():
        pytest.skip("расширение vector недоступно в этой сборке Postgres")

    # scripts/ не пакет — грузим модуль по пути
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "backfill_pgvector",
        Path(__file__).resolve().parents[2] / "scripts" / "backfill_pgvector.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    copy_batch, remaining = mod.copy_batch, mod.remaining

    from vera_shared.db.models import EventRow

    now = utc_naive_now()
    async with get_session() as s:
        for i, payload in enumerate(['[1,0,0]', '[0,1,0]', '"мусор"'], start=1):
            ev = EventRow(source="claude", source_event_id=f"e{i}", category="fact",
                          content_text="x", occurred_at=now, received_at=now,
                          triage_status="done")
            s.add(ev)
            await s.flush()
            await s.execute(sa_text(
                "INSERT INTO event_embeddings (event_id, embedding)"
                " VALUES (:i, CAST(:j AS jsonb))"), {"i": ev.id, "j": payload})

    assert await remaining() == 3
    assert await copy_batch(10) == 2          # третья битая — удалена
    assert await remaining() == 0
    assert await copy_batch(10) == 0          # повторный прогон ничего не делает


@pytest.mark.asyncio
async def test_remember_dedup_uses_the_vector_branch(pg_db, monkeypatch):
    """`_find_semantic_neighbour` на колонке vector: тот же вердикт, что и
    питоновский перебор, но одним обращением к индексу."""
    from sqlalchemy import text as sa_text
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow
    from vera_shared.db.vectors import as_pg_vector

    if not await _apply_pgvector_migration():
        pytest.skip("расширение vector недоступно в этой сборке Postgres")

    from gateway import claude as gc

    near, far = [0.999, 0.026, 0.0], [0.0, 1.0, 0.0]
    now = utc_naive_now()
    async with get_session() as s:
        for sid, vec in (("близкий", near), ("далёкий", far)):
            ev = EventRow(source="claude", source_event_id=sid, category="fact",
                          content_text=sid, occurred_at=now, received_at=now,
                          triage_status="done")
            s.add(ev)
            await s.flush()
            await s.execute(sa_text(
                "INSERT INTO event_embeddings (event_id, embedding, embedding_vec)"
                " VALUES (:i, CAST(:j AS jsonb), CAST(:v AS vector))"),
                {"i": ev.id, "j": str(vec), "v": as_pg_vector(vec)})
            if sid == "близкий":
                near_id = ev.id

    async def _embed(_t):
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr(gc, "embed", _embed)

    q_vec, match = await gc._find_semantic_neighbour("почти тот же факт")

    assert q_vec == [1.0, 0.0, 0.0]
    assert match is not None, "почти-дубль не найден через индекс"
    assert match[0] == near_id
    assert match[1] >= gc.SEMANTIC_DEDUP_THRESHOLD


@pytest.mark.asyncio
async def test_remember_dedup_returns_none_when_nothing_is_close(pg_db, monkeypatch):
    """Порог обязан работать в обе стороны: далёкий факт — не дубль."""
    from sqlalchemy import text as sa_text
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow
    from vera_shared.db.vectors import as_pg_vector

    if not await _apply_pgvector_migration():
        pytest.skip("расширение vector недоступно в этой сборке Postgres")

    from gateway import claude as gc

    now = utc_naive_now()
    async with get_session() as s:
        ev = EventRow(source="claude", source_event_id="далёкий", category="fact",
                      content_text="про другое", occurred_at=now, received_at=now,
                      triage_status="done")
        s.add(ev)
        await s.flush()
        await s.execute(sa_text(
            "INSERT INTO event_embeddings (event_id, embedding, embedding_vec)"
            " VALUES (:i, CAST(:j AS jsonb), CAST(:v AS vector))"),
            {"i": ev.id, "j": "[0,1,0]", "v": as_pg_vector([0.0, 1.0, 0.0])})

    monkeypatch.setattr(gc, "embed", lambda _t: _one([[1.0, 0.0, 0.0]]))

    _q, match = await gc._find_semantic_neighbour("совсем про другое")
    assert match is None


async def _one(value):
    return value


@pytest.mark.asyncio
async def test_triage_writes_both_columns_during_migration(pg_db, monkeypatch):
    """Пока идёт бэкфил, новые события обязаны попадать И в vector, И в JSONB —
    иначе они окажутся в дыре, которую бэкфил уже прошёл."""
    from unittest.mock import AsyncMock

    from sqlalchemy import text as sa_text
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    if not await _apply_pgvector_migration():
        pytest.skip("расширение vector недоступно в этой сборке Postgres")

    from brain_triage import worker

    now = utc_naive_now()
    async with get_session() as s:
        ev = EventRow(source="telegram", source_event_id="tg:1", account="userbot",
                      category="private", content_text="Игорь работает в Sintegrum",
                      occurred_at=now, received_at=now, triage_status="processing",
                      triage_started_at=now,
                      metadata_={"chat_kind": "private", "owner_participates": True})
        s.add(ev)
        await s.flush()
        await s.refresh(ev)
        s.expunge(ev)

    monkeypatch.setattr(worker, "is_backfill_paused", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "reserve_backfill_allowance", AsyncMock(return_value=None))
    monkeypatch.setattr(worker, "_claim_batch", AsyncMock(return_value=[ev]))
    monkeypatch.setattr(worker, "_embed_batch", AsyncMock(return_value=[[0.1, 0.2, 0.3]]))
    monkeypatch.setattr(worker, "apply_project_override", AsyncMock())
    monkeypatch.setattr(worker, "_safe_rel_extract", AsyncMock())
    monkeypatch.setattr(worker, "_process_one_with_sem",
                        AsyncMock(return_value=[(ev.id, "done", {"importance": 10}, None)]))
    monkeypatch.setattr(worker, "PACE_BETWEEN_S", 0)

    assert await worker.process_pending() == 1

    async with get_session() as s:
        row = (await s.execute(sa_text(
            "SELECT embedding, embedding_vec IS NOT NULL FROM event_embeddings"
            " WHERE event_id = :e"), {"e": ev.id})).one()
    assert row[0] == [0.1, 0.2, 0.3], "JSONB не записан"
    assert row[1] is True, "колонка vector не записана — событие выпадет из дедупа"

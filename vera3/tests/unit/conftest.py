"""Test-time env defaults for modules that read env at import.

Lives in unit/conftest.py so it runs BEFORE any test-module import,
keeping the test files themselves clean of stdlib/os env mutation
between import statements (which ruff I001 flags as broken ordering).

Здесь же в `sys.path` добавляются все `services/*/src` и `shared` — чтобы тесты
не зависели от того, какой `PYTHONPATH` собрал очередной шаг CI. Список путей
жил в трёх копиях в двух воркфлоу и разъехался: у шага diff-cover не было
`brain-triage`, и новый тест на импорте `brain_triage.project_override` валил
сборку тестов. Job quality краснел, деплой блокировался для ЧЕТЫРЁХ коммитов,
и никто не узнал — уведомление о падении живёт внутри job deploy, а тот при
красном гейте не запускается вовсе. Теперь список выводится из структуры
репозитория, поэтому новый сервис подхватывается сам.
"""
import os
import sys
from pathlib import Path

_VERA3 = Path(__file__).resolve().parents[2]
for _src in [_VERA3 / "shared", *sorted(_VERA3.glob("services/*/src"))]:
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TOKEN_SECRET", "0" * 44)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_vector_capability():
    """Кэш «есть ли таблица кусков / её индекс» живёт на процесс. В CI
    интеграционные тесты идут раньше юнит-тестов в том же процессе и оставляют
    в нём «таблица есть» (13.09.2026). Сбрасываем до и после каждого
    теста, а не только в фикстуре sqlite_db: многие тесты базу не берут."""
    from vera_shared.db.chunk_vectors import forget_chunk_capability
    from vera_shared.media_backlog import forget as forget_media_backlog
    forget_chunk_capability()
    forget_media_backlog()
    yield
    forget_media_backlog()
    forget_chunk_capability()


@pytest_asyncio.fixture
async def sqlite_db(tmp_path):
    """Файловая SQLite со всеми таблицами Base + чистый глобальный engine.

    Забирает на себя гигиену engine-глобалов: engine, оставленный ЧУЖИМ
    тестом, disposed на текущем loop'е (иначе его aiosqlite-тред умирает на
    закрытом loop'е позже — flaky «Event loop is closed» в случайном тесте).
    Yields get_session."""
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import (  # noqa: F401
        models,
        models_graph,
        models_links,
        models_mcp,
        models_sources,
        models_voice,
    )
    from vera_shared.db.chunk_vectors import forget_chunk_capability
    from vera_shared.db.engine import Base, get_session, init_engine
    from vera_shared.llm.circuit import forget_cooldowns

    # Кэши, которые живут в процессе и переживают базу теста. Оба уже
    # ловились на протечке: чужой открытый circuit блокировал вызов, которого
    # тест не ждал, а «колонка vector есть» из pg-теста уводила SQLite-тест в
    # ветку, которой на SQLite нет вовсе. Новая база — новое состояние.
    forget_cooldowns()
    forget_chunk_capability()

    if engine_mod._engine is not None:
        import contextlib
        with contextlib.suppress(Exception):
            await engine_mod._engine.dispose()
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None

    engine = await init_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield get_session
    await engine.dispose()
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None


@pytest.fixture(autouse=True)
def _fresh_broker_outage():
    """Брейкер сбоя брокера — состояние процесса: без сброса три отказа в
    одном тесте закрывали бы вызовы в следующих."""
    from vera_shared.llm.outage import reset_outage
    reset_outage()
    yield
    reset_outage()


@pytest_asyncio.fixture
async def ro_env(sqlite_db, monkeypatch):
    """`MCP_RO_DATABASE_URL` на той же SQLite, что у теста: sql_query ходит
    отдельным движком, а проверка роли на SQLite пропускается (роли там нет)."""
    import vera_shared.db.engine as engine_mod
    from vera_mcp.ro_engine import forget_ro_engine

    url = engine_mod._engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("MCP_RO_DATABASE_URL", url)
    await forget_ro_engine()
    yield
    await forget_ro_engine()

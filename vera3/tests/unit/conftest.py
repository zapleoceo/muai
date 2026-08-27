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

import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture
async def sqlite_db(tmp_path):
    """Файловая SQLite со всеми таблицами Base + чистый глобальный engine.

    Забирает на себя гигиену engine-глобалов: engine, оставленный ЧУЖИМ
    тестом, disposed на текущем loop'е (иначе его aiosqlite-тред умирает на
    закрытом loop'е позже — flaky «Event loop is closed» в случайном тесте).
    Yields get_session."""
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import models, models_graph, models_sources  # noqa: F401
    from vera_shared.db.engine import Base, get_session, init_engine

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

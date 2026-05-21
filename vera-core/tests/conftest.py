"""Pytest fixtures: isolated in-memory DB + a few sample tokens."""
import os
import tempfile

import pytest_asyncio

# Force a temp DB before any vera_shared import touches DB_PATH.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
os.environ["DB_PATH"] = _tmp_db.name
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-real-32-bytes-long-pad")
os.environ.setdefault("INTERNAL_SECRET", "test-internal")
os.environ.setdefault("DEPLOY_SECRET", "test-deploy")
os.environ.setdefault("OWNER_TELEGRAM_ID", "1")
os.environ.setdefault("VERA_GROUP_ID", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN_VERA", "1:fake")


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_schema():
    from vera_shared.db.engine import get_engine
    from vera_shared.db.migrations import run_migrations
    eng = get_engine()
    await run_migrations(eng)
    yield


@pytest_asyncio.fixture
async def sample_tokens():
    """Insert a couple of dummy active tokens, return their ids."""
    from vera_shared.tokens.repository import upsert
    t1 = await upsert("gemini", "test-gemini", "AIza-FAKE-1", ["chat:fast", "prefilter"])
    t2 = await upsert("deepseek", "test-ds", "sk-FAKE-2", ["chat:smart"])
    return [t1, t2]

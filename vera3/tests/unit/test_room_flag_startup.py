"""ROOM_SINGLE_EXECUTOR проверяется на старте сервисов: неверное значение валит запуск."""
from __future__ import annotations

import base64
import os

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from vera_shared.room import single_executor as se  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_flag():
    se.single_executor_mode.cache_clear()
    yield
    se.single_executor_mode.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["on", "freeze"])
async def test_dashboard_startup_fails_before_touching_the_database(monkeypatch, value):
    from dashboard.app import app
    monkeypatch.setenv(se.ENV, value)
    with pytest.raises(se.SingleExecutorUnsupported):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_dashboard_startup_rejects_garbage_value(monkeypatch):
    from dashboard.app import app
    monkeypatch.setenv(se.ENV, "maybe")
    with pytest.raises(ValueError, match=se.ENV):
        async with app.router.lifespan_context(app):
            pass


def test_mcp_main_fails_before_serving(monkeypatch):
    pytest.importorskip("standardwebhooks")
    from vera_mcp import __main__ as entry
    monkeypatch.setenv(se.ENV, "on")
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: pytest.fail("served"))
    with pytest.raises(se.SingleExecutorUnsupported):
        entry.main()

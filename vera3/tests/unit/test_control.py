"""vera_shared.control — runtime pause flag (DB mocked, Postgres-only SQL)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from vera_shared import control


class _FakeSession:
    def __init__(self, scalar=None):
        self.calls = []
        self._scalar = scalar

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        # Plain object so .scalar()/.scalar_one_or_none() are sync, not
        # AsyncMock coroutines.
        return SimpleNamespace(
            scalar=lambda: self._scalar,
            scalar_one_or_none=lambda: self._scalar,
        )


def test_backfill_flag_constant():
    assert control.BACKFILL_PAUSED == "backfill_paused"


@pytest.mark.asyncio
async def test_set_backfill_paused_true_writes_1():
    sess = _FakeSession()
    with patch.object(control, "get_session", lambda: sess):
        await control.set_backfill_paused(True)
    sql, params = sess.calls[0]
    assert "INSERT INTO app_control" in sql
    assert "ON CONFLICT" in sql
    assert params == {"k": "backfill_paused", "v": "1"}


@pytest.mark.asyncio
async def test_set_backfill_paused_false_writes_0():
    sess = _FakeSession()
    with patch.object(control, "get_session", lambda: sess):
        await control.set_backfill_paused(False)
    _, params = sess.calls[0]
    assert params["v"] == "0"


@pytest.mark.asyncio
async def test_is_backfill_paused_true_when_value_1():
    sess = _FakeSession(scalar="1")
    with patch.object(control, "get_session", lambda: sess):
        assert await control.is_backfill_paused() is True


@pytest.mark.asyncio
async def test_is_backfill_paused_false_when_value_0():
    sess = _FakeSession(scalar="0")
    with patch.object(control, "get_session", lambda: sess):
        assert await control.is_backfill_paused() is False


@pytest.mark.asyncio
async def test_is_backfill_paused_false_when_unset():
    """No row → default '0' → not paused (running by default)."""
    sess = _FakeSession(scalar=None)
    with patch.object(control, "get_session", lambda: sess):
        assert await control.is_backfill_paused() is False


@pytest.mark.asyncio
async def test_get_control_returns_default_when_missing():
    sess = _FakeSession(scalar=None)
    with patch.object(control, "get_session", lambda: sess):
        assert await control.get_control("nope", "fallback") == "fallback"


# ─── rate limit ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_max_per_hour_parses_int():
    sess = _FakeSession(scalar="600")
    with patch.object(control, "get_session", lambda: sess):
        assert await control.get_backfill_max_per_hour() == 600


@pytest.mark.asyncio
async def test_get_max_per_hour_garbage_is_zero():
    sess = _FakeSession(scalar="abc")
    with patch.object(control, "get_session", lambda: sess):
        assert await control.get_backfill_max_per_hour() == 0


@pytest.mark.asyncio
async def test_set_max_per_hour_clamps_negative():
    sess = _FakeSession()
    with patch.object(control, "get_session", lambda: sess):
        await control.set_backfill_max_per_hour(-50)
    assert sess.calls[0][1]["v"] == "0"


@pytest.mark.asyncio
async def test_reserve_none_when_unlimited():
    with patch.object(control, "get_backfill_max_per_hour", AsyncMock(return_value=0)):
        assert await control.reserve_backfill_allowance(16) is None


# ─── reserve_backfill_allowance на реальной SQLite (атомарный счётчик) ──────


@pytest_asyncio.fixture
async def db(sqlite_db):
    yield sqlite_db


@pytest.mark.asyncio
async def test_reserve_grants_then_exhausts_budget(db):
    # cap 600/h → 10/min: 6+4 выдаются, дальше 0 — даже из «другой реплики»
    with patch.object(control, "get_backfill_max_per_hour", AsyncMock(return_value=600)):
        assert await control.reserve_backfill_allowance(6) == 6
        assert await control.reserve_backfill_allowance(6) == 4
        assert await control.reserve_backfill_allowance(6) == 0


@pytest.mark.asyncio
async def test_reserve_floor_one_per_minute_for_small_cap(db):
    # cap 30/h округляется к <1/мин, но floor=1 — бэкфилл не встаёт намертво
    with patch.object(control, "get_backfill_max_per_hour", AsyncMock(return_value=30)):
        assert await control.reserve_backfill_allowance(16) == 1
        assert await control.reserve_backfill_allowance(16) == 0


@pytest.mark.asyncio
async def test_reserve_cleans_stale_minute_counters(db):
    from sqlalchemy import text
    from vera_shared.db.engine import get_session
    async with get_session() as s:   # set_control пишет now() — Postgres-only
        await s.execute(text(
            "INSERT INTO app_control (key, value, updated_at) "
            "VALUES ('backfill_used:200001010000', '99', CURRENT_TIMESTAMP)"))
    with patch.object(control, "get_backfill_max_per_hour", AsyncMock(return_value=600)):
        await control.reserve_backfill_allowance(1)
    async with get_session() as s:
        from sqlalchemy import text
        stale = (await s.execute(text(
            "SELECT COUNT(*) FROM app_control WHERE key = 'backfill_used:200001010000'"
        ))).scalar()
    assert stale == 0

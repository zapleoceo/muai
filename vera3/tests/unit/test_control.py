"""vera_shared.control — runtime pause flag (DB mocked, Postgres-only SQL)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
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
        res = AsyncMock()
        res.scalar_one_or_none = lambda: self._scalar
        return res


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
async def test_allowance_none_when_unlimited():
    with patch.object(control, "get_backfill_max_per_hour", AsyncMock(return_value=0)):
        assert await control.backfill_minute_allowance() is None


@pytest.mark.asyncio
async def test_allowance_remaining_under_budget():
    # cap 600/h → 10/min; 4 used this minute → 6 left
    with patch.object(control, "get_backfill_max_per_hour", AsyncMock(return_value=600)), \
         patch.object(control, "_requests_last_minute", AsyncMock(return_value=4)):
        assert await control.backfill_minute_allowance() == 6


@pytest.mark.asyncio
async def test_allowance_zero_when_budget_spent():
    with patch.object(control, "get_backfill_max_per_hour", AsyncMock(return_value=600)), \
         patch.object(control, "_requests_last_minute", AsyncMock(return_value=15)):
        assert await control.backfill_minute_allowance() == 0


@pytest.mark.asyncio
async def test_allowance_floor_one_per_minute_for_small_cap():
    # cap 30/h rounds to <1/min but floors to 1 so backfill never fully stalls
    with patch.object(control, "get_backfill_max_per_hour", AsyncMock(return_value=30)), \
         patch.object(control, "_requests_last_minute", AsyncMock(return_value=0)):
        assert await control.backfill_minute_allowance() == 1


@pytest.mark.asyncio
async def test_requests_last_minute_runs_count_query():
    sess = _FakeSession(scalar=7)
    with patch.object(control, "get_session", lambda: sess):
        n = await control._requests_last_minute()
    assert n == 7
    sql, params = sess.calls[0]
    assert "COUNT(*)" in sql and "usage_log" in sql
    assert params["wf"] == list(control._RATE_WORKFLOWS)

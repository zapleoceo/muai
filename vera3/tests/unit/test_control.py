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

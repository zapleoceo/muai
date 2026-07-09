"""vera_shared.db.engine — pool sizing helper.

`_pool_kwargs()` is split out of `init_engine()` specifically so it's
unit-testable without a real Postgres connection: `init_engine`'s pool-sizing
branch is Postgres-only (skipped for SQLite), and this test suite runs on
aiosqlite — same pattern as AIbroker's diff-cover/SQLite gate fix.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from vera_shared.db.engine import _pool_kwargs


def test_pool_kwargs_defaults():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DB_POOL_SIZE", None)
        os.environ.pop("DB_MAX_OVERFLOW", None)
        kw = _pool_kwargs()
    assert kw["pool_size"] == 3
    assert kw["max_overflow"] == 7
    assert kw["pool_pre_ping"] is True
    assert kw["pool_recycle"] == 1800
    assert kw["pool_timeout"] == 30


def test_pool_kwargs_reads_env_overrides():
    with patch.dict(os.environ, {"DB_POOL_SIZE": "10", "DB_MAX_OVERFLOW": "20"}):
        kw = _pool_kwargs()
    assert kw["pool_size"] == 10
    assert kw["max_overflow"] == 20

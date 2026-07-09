"""Тесты кэша дашборд-статистики (stale-while-revalidate)."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "dashboard", "src"))

from dashboard import stats  # noqa: E402


@pytest.mark.asyncio
async def test_cold_computes_synchronously():
    cache = {"value": None, "mono": 0.0}
    lock = asyncio.Lock()
    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        return {"v": calls["n"]}

    r = await stats._serve_cached(cache, lock, compute, force=False)
    assert r == {"v": 1}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_warm_hit_no_recompute():
    cache = {"value": None, "mono": 0.0}
    lock = asyncio.Lock()
    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        return {"v": calls["n"]}

    await stats._serve_cached(cache, lock, compute, force=False)   # cold → 1
    r = await stats._serve_cached(cache, lock, compute, force=False)  # warm → hit
    assert r == {"v": 1}
    assert calls["n"] == 1  # не пересчитывали


@pytest.mark.asyncio
async def test_stale_serves_old_and_refreshes_in_background(monkeypatch):
    cache = {"value": None, "mono": 0.0}
    lock = asyncio.Lock()
    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        return {"v": calls["n"]}

    await stats._serve_cached(cache, lock, compute, force=False)  # cold → v=1
    # искусственно состариваем кэш
    cache["mono"] -= (stats.TTL_S + 1)

    r = await stats._serve_cached(cache, lock, compute, force=False)
    assert r == {"v": 1}  # мгновенно отдали СТАРОЕ значение
    # фоновое обновление отработает на следующем тике event loop
    await asyncio.sleep(0.05)
    assert calls["n"] == 2  # в фоне пересчитали
    assert cache["value"] == {"v": 2}


@pytest.mark.asyncio
async def test_force_recomputes():
    cache = {"value": {"v": 99}, "mono": 9e18}  # «свежий» кэш
    lock = asyncio.Lock()

    async def compute():
        return {"v": 1}

    r = await stats._serve_cached(cache, lock, compute, force=True)
    assert r == {"v": 1}


@pytest.mark.asyncio
async def test_bg_refresh_failure_keeps_old_value(monkeypatch):
    cache = {"value": {"v": 7}, "mono": 0.0}
    lock = asyncio.Lock()
    cache["mono"] -= (stats.TTL_S + 1)  # protrukh

    async def compute():
        raise RuntimeError("db down")

    r = await stats._serve_cached(cache, lock, compute, force=False)
    assert r == {"v": 7}  # отдали старое
    await asyncio.sleep(0.05)
    assert cache["value"] == {"v": 7}  # обновление упало — старое сохранилось

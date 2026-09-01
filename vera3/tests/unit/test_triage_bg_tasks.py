"""Фоновые задачи триажа держатся под ссылками (ruff RUF006).

Событийный цикл хранит на задачу только слабую ссылку. Watchdog и
retry-цикл создавались голым create_task с отброшенным результатом — если
такая задача исчезнет, застрявшие в `processing` события не вернутся в
`pending` никогда. Тест фиксирует, что обе задачи попадают в _bg_tasks и
уходят оттуда по завершении.
"""
from __future__ import annotations

import asyncio

import pytest
from brain_triage import background_loops as bl


@pytest.mark.asyncio
async def test_start_background_loops_keeps_strong_refs():
    bl._bg_tasks.clear()
    tasks = bl.start_background_loops()
    try:
        assert len(tasks) == 2
        assert set(tasks) <= bl._bg_tasks, "задача не взята под ссылку"
        assert {t.get_name() for t in tasks} == {"triage-watchdog", "triage-retry"}
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_track_releases_reference_when_done():
    bl._bg_tasks.clear()

    async def noop() -> None:
        return None

    task = bl.track(asyncio.create_task(noop()))
    assert task in bl._bg_tasks
    await task
    # done_callback выполняется через call_soon — уступаем цикл
    await asyncio.sleep(0)
    assert task not in bl._bg_tasks, "ссылка не снята — набор растёт без предела"


@pytest.mark.asyncio
async def test_worker_starts_loops_under_reference():
    """main_loop() поднимает циклы через start_background_loops, а не голым
    create_task — иначе RUF006 вернётся ровно туда, откуда его убрали."""
    import inspect

    from brain_triage import worker

    src = inspect.getsource(worker.main_loop)
    assert "start_background_loops()" in src
    assert "create_task(_watchdog_loop" not in src
    assert "create_task(_retry_failed_loop" not in src

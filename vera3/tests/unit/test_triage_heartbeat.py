"""Отметка живости brain-triage для HEALTHCHECK.

У воркера нет HTTP-порта, поэтому healthcheck'а не было вовсе — а это
единственный сервис с репликами и с реальным сценарием «процесс жив, работы
не делает» (исчерпание пула фоновыми задачами). Для docker compose ps и для
монитора такая реплика выглядела здоровой: оба считают контейнеры и
рестарты, а не прогресс.
"""
from __future__ import annotations

import inspect

import pytest
from brain_triage import heartbeat as hb


@pytest.fixture(autouse=True)
def _beat_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "BEAT_FILE", tmp_path / "beat")


def test_missing_beat_counts_as_alive():
    """Контейнер только что поднялся, первый цикл ещё не закончил. На это
    есть start-period, ронять его healthcheck'ом нельзя."""
    assert hb.seconds_since_beat() is None
    assert hb.is_alive() is True


def test_fresh_beat_is_alive():
    hb.beat(now=1000.0)
    assert hb.seconds_since_beat(now=1000.5) == pytest.approx(0.5)
    assert hb.is_alive(now=1000.5) is True


def test_stale_beat_is_dead():
    hb.beat(now=1000.0)
    assert hb.is_alive(now=1000.0 + hb.STALE_AFTER_S + 1) is False


def test_boundary_is_exclusive():
    hb.beat(now=0.0)
    assert hb.is_alive(now=hb.STALE_AFTER_S - 0.1) is True
    assert hb.is_alive(now=hb.STALE_AFTER_S) is False


def test_beat_never_raises(monkeypatch, tmp_path):
    """Сбой записи отметки не имеет права останавливать разбор очереди."""
    monkeypatch.setattr(hb, "BEAT_FILE", tmp_path / "no-such-dir" / "beat")
    hb.beat()          # не бросает
    assert hb.seconds_since_beat() is None


def test_garbage_in_file_is_not_alive_by_accident(tmp_path, monkeypatch):
    f = tmp_path / "beat"
    f.write_text("не число")
    monkeypatch.setattr(hb, "BEAT_FILE", f)
    assert hb.seconds_since_beat() is None
    assert hb.is_alive() is True   # как «отметки ещё нет» — не притворяемся мёртвыми


def test_cli_exit_codes():
    hb.beat(now=hb.time.time())
    assert hb._cli() == 0
    hb.beat(now=hb.time.time() - hb.STALE_AFTER_S - 1)
    assert hb._cli() == 1


def test_stale_threshold_exceeds_slowest_triage_timeout():
    """Порог обязан быть заметно больше самого медленного нормального
    прохода, иначе healthcheck будет ронять ЗАНЯТЫЕ реплики."""
    assert hb.STALE_AFTER_S > 180        # групповой батч, concurrency.py


def test_main_loop_beats_every_iteration():
    """Отметка ставится в начале КАЖДОЙ итерации, до ветвлений: «жив» значит
    «цикл крутится», в том числе когда очередь пуста или circuit открыт."""
    from brain_triage import worker

    src = inspect.getsource(worker.main_loop)
    body = src.split("while True:", 1)[1]
    before_try = body.split("try:", 1)[0]
    assert "beat()" in before_try, "отметка не в начале итерации"


@pytest.mark.asyncio
async def test_main_loop_writes_beat_on_first_iteration(sqlite_db, monkeypatch, tmp_path):
    """Не только «строка на месте», но и что отметка реально появляется:
    именно на неё смотрит HEALTHCHECK контейнера."""
    import asyncio
    from unittest.mock import AsyncMock

    from brain_triage import background_loops as bl
    from brain_triage import triage_calls, worker

    beat_file = tmp_path / "beat"
    monkeypatch.setattr(hb, "BEAT_FILE", beat_file)
    monkeypatch.setattr(worker, "beat", hb.beat)

    seen = asyncio.Event()

    async def _one_pass() -> int:
        seen.set()
        await asyncio.sleep(3600)      # держим цикл, пока не отменим
        return 0

    monkeypatch.setattr(worker, "process_pending", _one_pass)
    monkeypatch.setattr(triage_calls, "resolve_triage_capability",
                        AsyncMock(return_value="chat:fast"))

    bl._bg_tasks.clear()
    task = asyncio.create_task(worker.main_loop())
    try:
        await asyncio.wait_for(seen.wait(), timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for t in list(bl._bg_tasks):
            t.cancel()
        await asyncio.gather(*list(bl._bg_tasks), return_exceptions=True)

    assert beat_file.exists(), "HEALTHCHECK не увидел бы ни одной отметки"
    assert hb.is_alive()

"""Запись строк журнала /mcp в БД вне пути запроса: fail-open, память ограничена."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from vera_mcp import request_log_repo

log = logging.getLogger("vera_mcp.request")

MAX_PENDING = 100
MAX_CONCURRENT_WRITES = 2
WRITE_TIMEOUT_S = 3.0
WARN_EVERY_S = 60.0

# Задача на строку, а не очередь с фоновым писателем: у очереди своя привязка к
# event loop и жизненный цикл запуска/остановки; здесь достаточно удержать ссылку,
# чтобы задачу не собрал GC, и ограничить число живых задач. Пул БД общий с
# room-инструментами: семафор и таймаут не дают журналу занять соединения при медленной БД.
_pending: set[asyncio.Task[None]] = set()
_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_state: dict[str, float] = {"dropped": 0, "failed": 0, "last_warn": float("-inf")}


def _warn_rate_limited(reason: str, exc: BaseException | None = None) -> None:
    now = time.monotonic()
    if now - _state["last_warn"] < WARN_EVERY_S:
        return
    lost = int(_state["dropped"] + _state["failed"])
    _state["last_warn"] = now
    log.warning("mcp_request_log persist %s; lost_total=%d err=%s",
                reason, lost, type(exc).__name__ if exc else "-")


def _semaphore() -> asyncio.Semaphore:
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or _gate[0] is not loop:
        _gate = (loop, asyncio.Semaphore(MAX_CONCURRENT_WRITES))
    return _gate[1]


async def _guarded_insert(row: dict[str, Any]) -> None:
    async with _semaphore():
        await request_log_repo.insert_request(row)


async def _write(row: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(_guarded_insert(row), WRITE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — журнал не должен влиять на ответ
        _state["failed"] += 1
        _warn_rate_limited("timeout" if isinstance(exc, TimeoutError) else "failed", exc)


def schedule_insert(row: dict[str, Any]) -> None:
    """Не ждёт записи и не бросает: при переполнении или без loop строка теряется."""
    if len(_pending) >= MAX_PENDING:
        _state["dropped"] += 1
        _warn_rate_limited("overflow")
        return
    try:
        task = asyncio.get_running_loop().create_task(_write(row))
    except RuntimeError:
        _state["dropped"] += 1
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)

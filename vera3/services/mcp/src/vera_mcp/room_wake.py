"""Сигнал «в комнате новое сообщение» для блокирующего room_inbox_wait.

Источник сигнала — Postgres LISTEN на канале `room_messages`: NOTIFY шлёт сама запись
сообщения (`vera_shared.room.messages`) в той же транзакции, поэтому его видно из любого
сервиса — дашборда, бота, MCP. Свой room_post дёргает `bump()` ещё и напрямую.
Сигнал только будит ожидающих: что именно пришло, они перечитывают из БД.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg
from vera_shared.db.engine import database_url
from vera_shared.room.messages import NOTIFY_CHANNEL

log = logging.getLogger(__name__)

_version = 0
_cond: asyncio.Condition | None = None
_conn: Any = None
_connect_lock: asyncio.Lock | None = None
_loop: asyncio.AbstractEventLoop | None = None


def version() -> int:
    return _version


def _condition() -> asyncio.Condition:
    """Примитивы asyncio и соединение asyncpg привязаны к циклу: на новом цикле — новые."""
    global _cond, _conn, _connect_lock, _loop
    loop = asyncio.get_running_loop()
    if _cond is None or _loop is not loop:
        _cond, _conn, _connect_lock, _loop = asyncio.Condition(), None, asyncio.Lock(), loop
    return _cond


async def bump() -> None:
    global _version
    async with _condition():
        _version += 1
        _condition().notify_all()


def _on_notify(_conn: Any, _pid: int, _channel: str, _payload: str) -> None:
    asyncio.get_running_loop().create_task(bump())


def _on_close(_closed: Any) -> None:
    global _conn
    _conn = None


async def _ensure_listener() -> None:
    global _conn
    url = database_url()
    if not url.startswith("postgresql"):
        return
    _condition()
    if _conn is not None and not _conn.is_closed():
        return
    assert _connect_lock is not None
    async with _connect_lock:
        if _conn is not None and not _conn.is_closed():
            return
        try:
            conn = await asyncpg.connect(url.replace("+asyncpg", "", 1), timeout=10)
            await conn.add_listener(NOTIFY_CHANNEL, _on_notify)
            conn.add_termination_listener(_on_close)
        except (OSError, asyncpg.PostgresError, TimeoutError) as e:
            log.warning("room wake: LISTEN unavailable, falling back to recheck: %s",
                        type(e).__name__)
            return
        _conn = conn


async def wait_since(seen: int, timeout: float) -> bool:
    """True, если с версии `seen` пришёл сигнал; False по таймауту."""
    await _ensure_listener()
    cond = _condition()
    async with cond:
        try:
            await asyncio.wait_for(cond.wait_for(lambda: _version != seen), timeout)
        except TimeoutError:
            return False
    return True

"""Клиент MCP stdio: дочерний процесс, JSON-RPC построчно.

Процесс стартует при первом вызове и перезапускается, если умер. Ответы
разбирает одна читающая задача и раздаёт их по id. stderr процесса уходит в
никуда: туда stdio-серверы любят печатать токены и тела запросов.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
from typing import Any

from vera_hub.errors import UpstreamError
from vera_hub.upstream_http import CLIENT_INFO, PROTOCOL_VERSION

MAX_LINE = 8 * 1024 * 1024
# базовое окружение: без env хаба (там HUB_SECRET_KEY и DATABASE_URL)
_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


class _PipeClosed(UpstreamError):
    """Запрос не дошёл до процесса: повтор с новым процессом безопасен."""

    def __init__(self) -> None:
        super().__init__("down", "stdio process closed stdin")


class StdioUpstream:
    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self._command, self._env = command, env
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._ready = False

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _start(self) -> None:
        env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ} | self._env
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, limit=MAX_LINE)
        except OSError as exc:
            raise UpstreamError("down", f"stdio command failed to start: {type(exc).__name__}") from exc
        self._reader = asyncio.create_task(self._read_loop(self._proc))
        result = await self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO})
        if not isinstance(result, dict):
            raise UpstreamError("protocol", "stdio initialize returned no result")
        await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ready = True

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while line := await proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                fut = self._pending.pop(msg.get("id"), None) if isinstance(msg, dict) else None
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except (ValueError, asyncio.LimitOverrunError):
            pass  # строка длиннее MAX_LINE: процесс считаем сломанным ниже
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(UpstreamError("down", "stdio process exited"))
            self._pending.clear()

    async def _write(self, payload: dict[str, Any]) -> None:
        if not self._alive() or self._proc is None or self._proc.stdin is None:
            raise _PipeClosed
        self._proc.stdin.write(json.dumps(payload).encode() + b"\n")
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _PipeClosed from exc

    async def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            msg = await fut
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise UpstreamError("rpc", str(err.get("message", "upstream error")),
                                int(err.get("code", -32000)))
        result = msg.get("result")
        if not isinstance(result, dict):
            raise UpstreamError("protocol", "upstream result is not an object")
        return result

    async def request(self, method: str, params: dict[str, Any],
                      extra: dict[str, str] | None = None) -> dict[str, Any]:
        del extra  # заголовков у stdio нет
        for attempt in (1, 2):
            async with self._lock:
                # оборванный таймаутом initialize оставляет живой, но не готовый процесс
                if not self._alive() or not self._ready:
                    await self.close()
                    await self._start()
            try:
                return await self._send(method, params)
            except _PipeClosed:
                # процесс умер, а returncode ещё не обновился: перезапуск и один повтор
                self._ready = False
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    async def close(self) -> None:
        proc, self._proc, self._ready = self._proc, None, False
        if proc is not None and proc.returncode is None:
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None

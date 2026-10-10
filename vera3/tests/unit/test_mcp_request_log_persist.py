"""Персистентный журнал /mcp: вставка вне пути запроса, fail-open, лимиты, без секретов."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from vera_mcp import request_log_store as store
from vera_mcp.auth import CLIENT_SCOPE_KEY
from vera_mcp.request_log import RequestLogMiddleware

CANARY = "sk-live-CANARY-9f8e7d"


@pytest.fixture(autouse=True)
def _reset_store(monkeypatch):
    monkeypatch.setattr(store, "_state", {"dropped": 0, "failed": 0, "last_warn": float("-inf")})
    monkeypatch.setattr(store, "_pending", set())
    monkeypatch.setattr(store, "_gate", None)


async def _app(scope, receive, send) -> None:
    scope[CLIENT_SCOPE_KEY] = "claude"
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


async def _call(headers=None, body: bytes | None = None) -> list[dict]:
    payload = body if body is not None else json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "room_post", "arguments": {"text": CANARY}}}).encode()
    msgs = [{"type": "http.request", "body": payload, "more_body": False}]
    sent: list[dict] = []

    async def receive() -> dict:
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    async def send(m: dict) -> None:
        sent.append(m)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers or []}
    await RequestLogMiddleware(_app)(scope, receive, send)
    return sent


async def _drain() -> None:
    await asyncio.gather(*list(store._pending))


def _capture(monkeypatch) -> list[dict]:
    rows: list[dict] = []

    async def fake_insert(row: dict) -> None:
        rows.append(row)

    monkeypatch.setattr(store.request_log_repo, "insert_request", fake_insert)
    return rows


@pytest.mark.asyncio
async def test_insert_scheduled_with_sanitized_fields(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    await _call([(b"x-request-id", b"cid-1"), (b"user-agent", b"cl ient/1;\n")])
    await _drain()
    (row,) = rows
    assert row["cid"] == "cid-1"
    assert (row["method"], row["actor"], row["rpc"], row["tool"], row["rpc_id"]) == (
        "POST", "claude", "tools/call", "room_post", "7")
    assert row["ua"] == "cl_ient/1__"
    assert (row["status"], row["outcome"]) == (200, "ok")
    assert isinstance(row["ms"], int)
    assert set(row) == {"cid", "method", "actor", "ua", "rpc", "tool", "rpc_id",
                        "status", "ms", "outcome"}


@pytest.mark.asyncio
async def test_no_secret_canary_in_insert_params(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    await _call([(b"authorization", f"Bearer {CANARY}".encode()),
                 (b"cookie", f"s={CANARY}".encode())])
    await _drain()
    assert rows
    assert CANARY not in json.dumps(rows)


@pytest.mark.asyncio
async def test_lengths_enforced(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    big = json.dumps({"jsonrpc": "2.0", "id": "i" * 100, "method": "m" * 200,
                      "params": {"name": "t" * 200}}).encode()
    await _call([(b"user-agent", b"u" * 300)], body=big)
    await _drain()
    row = rows[0]
    assert len(row["ua"]) == 80
    assert len(row["rpc"]) == 64
    assert len(row["rpc_id"]) == 32


@pytest.mark.asyncio
async def test_db_failure_does_not_affect_response_and_warns_once(
        monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    async def boom(row: dict) -> None:
        raise ConnectionError("db down")

    monkeypatch.setattr(store.request_log_repo, "insert_request", boom)
    caplog.set_level(logging.WARNING, logger="vera_mcp.request")
    for _ in range(5):
        sent = await _call()
        assert sent[0]["status"] == 200
    await _drain()
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    assert CANARY not in warns[0].getMessage()
    assert store._state["failed"] == 5


@pytest.mark.asyncio
async def test_overflow_drops_without_blocking(monkeypatch) -> None:
    gate = asyncio.Event()
    calls: list[dict] = []

    async def slow(row: dict) -> None:
        await gate.wait()
        calls.append(row)

    monkeypatch.setattr(store.request_log_repo, "insert_request", slow)
    monkeypatch.setattr(store, "MAX_PENDING", 3)
    for _ in range(10):
        await asyncio.wait_for(_call(), timeout=1)
    assert len(store._pending) == 3
    assert store._state["dropped"] == 7
    gate.set()
    await _drain()
    assert len(calls) == 3


def test_schedule_without_running_loop_drops_silently() -> None:
    store.schedule_insert({"cid": "x"})
    assert store._state["dropped"] == 1


@pytest.mark.asyncio
async def test_hanging_insert_limits_concurrency_and_never_blocks_request(monkeypatch) -> None:
    gate = asyncio.Event()
    active = peak = 0

    async def slow(row: dict) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await gate.wait()
        finally:
            active -= 1

    monkeypatch.setattr(store.request_log_repo, "insert_request", slow)
    monkeypatch.setattr(store, "MAX_PENDING", 5)
    for _ in range(12):
        await asyncio.wait_for(_call(), timeout=1)
    await asyncio.sleep(0)
    assert peak == store.MAX_CONCURRENT_WRITES == 2
    assert len(store._pending) == 5
    assert store._state["dropped"] == 7
    gate.set()
    await _drain()
    assert peak == 2


@pytest.mark.asyncio
async def test_write_timeout_drops_row_and_warns(
        monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    async def hang(row: dict) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(store.request_log_repo, "insert_request", hang)
    monkeypatch.setattr(store, "WRITE_TIMEOUT_S", 0.05)
    caplog.set_level(logging.WARNING, logger="vera_mcp.request")
    await _call()
    await _drain()
    assert store._state["failed"] == 1
    assert not store._pending
    assert "timeout" in caplog.records[-1].getMessage()

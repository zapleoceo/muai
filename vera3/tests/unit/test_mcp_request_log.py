"""Журнал запросов /mcp: cid, разбор JSON-RPC без утечки аргументов, исходы."""
from __future__ import annotations

import json
import logging

import pytest
from vera_mcp.auth import CLIENT_SCOPE_KEY
from vera_mcp.request_log import RequestLogMiddleware, pick_cid

SECRET = "sk-live-SUPERSECRET-123"


def _scope(headers: list[tuple[bytes, bytes]] | None = None, path: str = "/mcp") -> dict:
    return {"type": "http", "method": "POST", "path": path, "headers": headers or []}


def _rpc_body(method: str = "tools/call", params: dict | None = None) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 7, "method": method,
                       "params": params if params is not None else {}}).encode()


class Harness:
    def __init__(self, body: bytes, extra: list[dict] | None = None) -> None:
        self.messages = [{"type": "http.request", "body": body, "more_body": False},
                         *(extra or [])]
        self.sent: list[dict] = []

    async def receive(self) -> dict:
        if self.messages:
            return self.messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(self, message: dict) -> None:
        self.sent.append(message)


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "vera_mcp.request" and r.levelno == logging.INFO]


async def _ok_app(scope, receive, send) -> None:
    scope[CLIENT_SCOPE_KEY] = "claude"
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


@pytest.mark.asyncio
async def test_cid_generated_and_echoed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="vera_mcp.request")
    h = Harness(_rpc_body("tools/list"))
    await RequestLogMiddleware(_ok_app)(_scope(), h.receive, h.send)
    cid = dict(h.sent[0]["headers"])[b"x-request-id"].decode()
    (line,) = _lines(caplog)
    assert len(cid) == 16
    assert f"cid={cid} " in line
    assert "actor=claude" in line
    assert "rpc=tools/list" in line
    assert "rpc_id=7" in line
    assert "status=200" in line
    assert "outcome=ok" in line
    assert any("mcp_start" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_incoming_request_id_honoured_and_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")
    assert pick_cid([(b"X-Request-ID", b"abc-123.x_y")]) == "abc-123.x_y"
    assert pick_cid([(b"x-correlation-id", b"corr-1")]) == "corr-1"
    for bad in (b"a b", b"x" * 65, b"inj\nected", b""):
        assert len(pick_cid([(b"x-request-id", bad)])) == 16
    h = Harness(_rpc_body("ping"))
    await RequestLogMiddleware(_ok_app)(_scope([(b"x-request-id", b"abc-123")]), h.receive, h.send)
    assert dict(h.sent[0]["headers"])[b"x-request-id"] == b"abc-123"
    assert "cid=abc-123 " in _lines(caplog)[0]


@pytest.mark.asyncio
async def test_tool_name_logged_arguments_never(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="vera_mcp.request")
    body = _rpc_body(params={"name": "room_post", "arguments": {"text": SECRET}})
    h = Harness(body)
    await RequestLogMiddleware(_ok_app)(_scope(), h.receive, h.send)
    (line,) = _lines(caplog)
    assert "tool=room_post" in line
    assert all("SUPERSECRET" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_body_replayed_byte_identical() -> None:
    body = _rpc_body(params={"name": "t", "arguments": {"x": SECRET}})
    seen: list[bytes] = []

    async def app(scope, receive, send) -> None:
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            seen.append(msg["body"])
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    h = Harness(body)
    await RequestLogMiddleware(app)(_scope(), h.receive, h.send)
    assert b"".join(seen) == body


@pytest.mark.asyncio
async def test_client_disconnect_outcome(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")

    async def app(scope, receive, send) -> None:
        await receive()
        assert (await receive())["type"] == "http.disconnect"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})

    h = Harness(_rpc_body("tools/call", {"name": "slow"}))
    await RequestLogMiddleware(app)(_scope(), h.receive, h.send)
    assert "outcome=client_disconnected" in _lines(caplog)[0]


@pytest.mark.asyncio
async def test_exception_logged_and_reraised(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")

    async def app(scope, receive, send) -> None:
        raise RuntimeError("boom")

    h = Harness(_rpc_body("ping"))
    with pytest.raises(RuntimeError, match="boom"):
        await RequestLogMiddleware(app)(_scope(), h.receive, h.send)
    line = _lines(caplog)[0]
    assert "outcome=server_exception" in line
    assert "status=500" in line


@pytest.mark.asyncio
async def test_unauthorized_logged_with_dash_actor(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")

    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    h = Harness(_rpc_body("ping"))
    await RequestLogMiddleware(app)(_scope(), h.receive, h.send)
    line = _lines(caplog)[0]
    assert "actor=-" in line
    assert "status=401" in line
    assert "outcome=http_error" in line


@pytest.mark.asyncio
async def test_other_paths_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")
    h = Harness(b"")
    await RequestLogMiddleware(_ok_app)(_scope(path="/healthz"), h.receive, h.send)
    assert _lines(caplog) == []

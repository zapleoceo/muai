"""Враждебные проверки журнала /mcp: утечки, replay тела, спуфинг, инъекции в лог."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from vera_mcp.auth import CLIENT_SCOPE_KEY
from vera_mcp.request_log import BODY_PEEK_LIMIT, RequestLogMiddleware, pick_cid

CANARY = "CANARY-sk-live-9f8e7d"


def _scope(headers: list[tuple[bytes, bytes]] | None = None, method: str = "POST",
           path: str = "/mcp", query: bytes = b"") -> dict:
    return {"type": "http", "method": method, "path": path, "headers": headers or [],
            "query_string": query}


class Feed:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = list(messages)
        self.sent: list[dict] = []

    async def receive(self) -> dict:
        return self.messages.pop(0) if self.messages else {"type": "http.disconnect"}

    async def send(self, message: dict) -> None:
        self.sent.append(message)


def _req(body: bytes, more: bool = False) -> dict:
    return {"type": "http.request", "body": body, "more_body": more}


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "vera_mcp.request"]


def _info(caplog: pytest.LogCaptureFixture) -> str:
    (rec,) = (r for r in _records(caplog) if r.levelno == logging.INFO)
    return rec.getMessage()


async def _ok(scope, receive, send) -> None:
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


def _all_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() + str(r.args) + str(r.exc_text) for r in caplog.records)


# 1. утечки
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "t", "arguments": {"password": CANARY, "token": CANARY}}}).encode(),
    json.dumps([{"id": 1, "method": "tools/call", "params": {
        "name": "t", "arguments": {"api_key": CANARY}}}, {"x": CANARY}]).encode(),
    b'{"method": "tools/call", "params": {"name": "t", "arguments": {"s": "' + CANARY.encode(),
    CANARY.encode(),
    json.dumps({"id": CANARY, "method": "m", "params": {"authorization": CANARY}}).encode()[:-1],
])
async def test_no_body_secrets_in_logs(caplog: pytest.LogCaptureFixture, body: bytes) -> None:
    caplog.set_level(logging.DEBUG, logger="vera_mcp.request")
    f = Feed([_req(body)])
    hdr = [(b"authorization", f"Bearer {CANARY}".encode()), (b"cookie", CANARY.encode())]
    await RequestLogMiddleware(_ok)(_scope(hdr), f.receive, f.send)
    assert CANARY not in _all_text(caplog)


@pytest.mark.asyncio
async def test_no_secrets_on_exception_path(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="vera_mcp.request")

    async def boom(scope, receive, send) -> None:
        raise RuntimeError("fail")

    f = Feed([_req(json.dumps({"method": "x", "params": {"arguments": CANARY}}).encode())])
    hdr = [(b"authorization", f"Bearer {CANARY}".encode())]
    with pytest.raises(RuntimeError):
        await RequestLogMiddleware(boom)(_scope(hdr), f.receive, f.send)
    assert CANARY not in _all_text(caplog)


@pytest.mark.asyncio
async def test_log_failure_on_pathological_json_does_not_mask_response(
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")
    f = Feed([_req(b"[" * 60000)])
    await RequestLogMiddleware(_ok)(_scope(), f.receive, f.send)
    assert f.sent[0]["status"] == 200
    assert "rpc=-" in _info(caplog)


# 2. peek / replay
@pytest.mark.asyncio
async def test_large_multichunk_body_identical_and_ordered() -> None:
    chunks = [bytes([i]) * 20000 for i in range(1, 8)]
    msgs = [_req(c, more=i < len(chunks) - 1) for i, c in enumerate(chunks)]
    got: list[bytes] = []

    async def app(scope, receive, send) -> None:
        while True:
            m = await receive()
            got.append(m["body"])
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    f = Feed(msgs)
    await RequestLogMiddleware(app)(_scope(), f.receive, f.send)
    assert got == chunks
    assert sum(map(len, got)) > BODY_PEEK_LIMIT


@pytest.mark.asyncio
async def test_disconnect_during_peek_is_replayed_and_reported(
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")
    types: list[str] = []

    async def app(scope, receive, send) -> None:
        for _ in range(3):
            types.append((await receive())["type"])
        await send({"type": "http.response.start", "status": 200, "headers": []})

    f = Feed([_req(b"{", more=True)])
    await RequestLogMiddleware(app)(_scope(), f.receive, f.send)
    assert types == ["http.request", "http.disconnect", "http.disconnect"]
    assert "outcome=client_disconnected" in _info(caplog)


@pytest.mark.asyncio
async def test_inner_reads_beyond_replay_reach_real_receive() -> None:
    f = Feed([_req(b"A" * 10, more=True), _req(b"TAIL")])
    seen: list[bytes] = []

    async def app(scope, receive, send) -> None:
        for _ in range(2):
            seen.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await RequestLogMiddleware(app)(_scope(), f.receive, f.send)
    assert seen == [b"A" * 10, b"TAIL"]


@pytest.mark.asyncio
async def test_get_does_not_consume_receive() -> None:
    f = Feed([_req(b"keep")])
    got: list[bytes] = []

    async def app(scope, receive, send) -> None:
        got.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await RequestLogMiddleware(app)(_scope(method="GET"), f.receive, f.send)
    assert got == [b"keep"]


@pytest.mark.asyncio
async def test_cancellation_propagates_and_is_not_logged_as_ok(
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")

    async def app(scope, receive, send) -> None:
        raise asyncio.CancelledError

    f = Feed([_req(b"{}")])
    with pytest.raises(asyncio.CancelledError):
        await RequestLogMiddleware(app)(_scope(), f.receive, f.send)
    assert "outcome=ok" not in _info(caplog)


# 3. actor
@pytest.mark.asyncio
async def test_actor_not_spoofable_by_client(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")
    hdr = [(b"x-mcp-client", b"owner"), (b"mcp_client", b"owner")]
    f = Feed([_req(b"{}")])
    await RequestLogMiddleware(_ok)(_scope(hdr, query=b"mcp_client=owner"), f.receive, f.send)
    line = _info(caplog)
    assert "actor=-" in line
    assert "owner" not in line.split("ua=")[0]


@pytest.mark.asyncio
async def test_actor_comes_from_scope_set_by_inner_auth(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")

    async def app(scope, receive, send) -> None:
        scope[CLIENT_SCOPE_KEY] = "codex"
        await _ok(scope, receive, send)

    f = Feed([_req(b"{}")])
    await RequestLogMiddleware(app)(_scope([(b"x-mcp-client", b"owner")]), f.receive, f.send)
    assert "actor=codex " in _info(caplog)


# 4. инъекции
@pytest.mark.parametrize("bad", [b"a\r\nb", b"a b", b'a"b', b"a=b", b"a\nb", b"a,b", b"a;b",
                                 b" a", b"a ", b"a\n", b"x" * 65, "я".encode(), b"a%0ab"])
def test_pick_cid_rejects_unsafe(bad: bytes) -> None:
    out = pick_cid([(b"x-request-id", bad)])
    assert out != bad.decode("latin-1")
    assert len(out) == 16
    assert out.isalnum()


def test_pick_cid_accepts_64_and_falls_back_to_correlation() -> None:
    assert pick_cid([(b"x-request-id", b"a" * 64)]) == "a" * 64
    assert pick_cid([(b"x-request-id", b"bad id"), (b"x-correlation-id", b"ok-1")]) == "ok-1"


@pytest.mark.asyncio
async def test_no_fake_key_value_via_ua_tool_rpc_id(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")
    evil = "x\nstatus=200 actor=owner outcome=ok =;\r\n\"q\""
    body = json.dumps({"id": evil, "method": "tools/call", "params": {"name": evil}}).encode()
    f = Feed([_req(body)])
    await RequestLogMiddleware(_ok)(_scope([(b"user-agent", evil.encode())]), f.receive, f.send)
    line = _info(caplog)
    assert "\n" not in line
    assert "\r" not in line
    assert line.count(" status=") == 1
    assert line.count(" actor=") == 1
    assert line.count(" outcome=") == 1
    assert line.count("=") == 11  # только ключи самого журнала


@pytest.mark.asyncio
async def test_rpc_method_injection_cleaned(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vera_mcp.request")
    f = Feed([_req(json.dumps({"id": 1, "method": "a b=c\nd"}).encode())])
    await RequestLogMiddleware(_ok)(_scope(), f.receive, f.send)
    assert "rpc=a_b_c_d " in _info(caplog)


# 5. формулировки и прочее
@pytest.mark.asyncio
async def test_wording_is_transport_facts_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="vera_mcp.request")
    f = Feed([_req(b"{}")])
    await RequestLogMiddleware(_ok)(_scope(), f.receive, f.send)
    text = _all_text(caplog).lower()
    assert "user" not in text
    assert "cancel" not in text


@pytest.mark.asyncio
async def test_request_id_header_not_duplicated_and_replaced() -> None:
    async def app(scope, receive, send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"X-Request-ID", b"inner"), (b"x-other", b"1")]})
        await send({"type": "http.response.body", "body": b""})

    f = Feed([_req(b"{}")])
    await RequestLogMiddleware(app)(_scope([(b"x-request-id", b"mine-1")]), f.receive, f.send)
    hdrs = f.sent[0]["headers"]
    assert [v for k, v in hdrs if k.lower() == b"x-request-id"] == [b"mine-1"]
    assert (b"x-other", b"1") in hdrs


@pytest.mark.asyncio
async def test_non_mcp_and_non_http_scopes_untouched(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="vera_mcp.request")
    for scope in (_scope(path="/healthz"), _scope(path="/mcp/x"), {"type": "lifespan"},
                  {"type": "websocket", "path": "/mcp"}):
        f = Feed([_req(b"{}")])

        async def app(scope_, receive, send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})

        await RequestLogMiddleware(app)(scope, f.receive, f.send)
        assert f.sent == [{"type": "http.response.start", "status": 200, "headers": []}]
        assert f.messages  # тело не тронуто
    assert _records(caplog) == []

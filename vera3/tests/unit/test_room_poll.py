"""Режим poll слушателя: повторы при обрывах, выход при отказе токена, общий срок, SSE."""
from __future__ import annotations

import io
import json
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import room_mcp_client as client  # noqa: E402
import room_poll as rp  # noqa: E402

URL = "https://vera.test/mcp"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "x", {}, io.BytesIO())  # type: ignore[arg-type]


def _script(steps: list[Any], clock: Clock, calls: list[dict[str, Any]]):
    def call(_url, _token, name, args, timeout):
        assert name == "room_inbox_wait"
        assert timeout > args["timeout_s"]
        calls.append(args)
        clock.now += args["timeout_s"] if not steps else 0.5
        step = steps.pop(0) if steps else {"messages": [], "timed_out": True}
        if isinstance(step, Exception):
            raise step
        return step
    return call


def test_returns_first_batch_and_keeps_after_fixed() -> None:
    clock, calls = Clock(), []
    steps: list[Any] = [{"messages": [], "timed_out": True},
                        {"messages": [{"id": 7}], "timed_out": False}]
    code, res = rp.poll(URL, "t", 5, 600, call=_script(steps, clock, calls),
                        sleep=clock.sleep, clock=clock)
    assert code == rp.EXIT_OK and res["messages"] == [{"id": 7}]
    assert [c["after_id"] for c in calls] == [5, 5]
    assert clock.sleeps == []


def test_network_drops_and_overload_are_retried_with_backoff() -> None:
    clock, calls = Clock(), []
    steps: list[Any] = [urllib.error.URLError("reset"), ConnectionResetError(), _http(429),
                        _http(502), TimeoutError(), {"messages": [{"id": 1}]}]
    code, _ = rp.poll(URL, "t", None, 600, call=_script(steps, clock, calls),
                      sleep=clock.sleep, clock=clock)
    assert code == rp.EXIT_OK
    assert clock.sleeps == [1.0, 2.0, 5.0, 10.0, 30.0]
    assert all("after_id" not in c for c in calls)


def test_backoff_resets_after_success() -> None:
    clock, calls = Clock(), []
    steps: list[Any] = [ConnectionResetError(), {"messages": []}, ConnectionResetError(),
                        {"messages": [{"id": 2}]}]
    rp.poll(URL, "t", None, 600, call=_script(steps, clock, calls),
            sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [1.0, 1.0]


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_token_exits_without_retry(code: int) -> None:
    clock, calls = Clock(), []
    result = rp.poll(URL, "t", None, 600, call=_script([_http(code)], clock, calls),
                     sleep=clock.sleep, clock=clock)
    assert result == (rp.EXIT_AUTH, None) and len(calls) == 1


def test_overall_timeout_caps_each_request() -> None:
    clock, calls = Clock(), []
    code, _ = rp.poll(URL, "t", None, 100, call=_script([], clock, calls),
                      sleep=clock.sleep, clock=clock)
    assert code == rp.EXIT_TIMEOUT
    assert [c["timeout_s"] for c in calls] == [80, 20]


def test_parse_body_accepts_json_and_sse() -> None:
    payload = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"a": 1}}}
    assert client.parse_body(json.dumps(payload).encode()) == payload
    sse = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
    assert client.parse_body(sse) == payload
    with pytest.raises(ValueError):
        client.parse_body(b"event: ping\n\n")


@pytest.fixture
def fake_mcp():
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a: Any) -> None:
            return None

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"auth": self.headers["Authorization"],
                         "session": self.headers.get("Mcp-Session-Id"), "body": body})
            if body["method"] == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if body["method"] == "initialize":
                out = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}}
            else:
                out = {"jsonrpc": "2.0", "id": 2, "result": {
                    "content": [{"type": "text", "text": "{}"}],
                    "structuredContent": {"messages": [{"id": 9}], "timed_out": False}}}
            data = f"event: message\ndata: {json.dumps(out)}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "s-1")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp", seen
    server.shutdown()


def test_call_tool_speaks_mcp_over_sse(fake_mcp) -> None:
    url, seen = fake_mcp
    res = client.call_tool(url, "tok", "room_inbox_wait", {"timeout_s": 5}, 10)
    assert res == {"messages": [{"id": 9}], "timed_out": False}
    assert [s["body"]["method"] for s in seen] == [
        "initialize", "notifications/initialized", "tools/call"]
    assert [s["session"] for s in seen] == [None, "s-1", "s-1"]
    assert all(s["auth"] == "Bearer tok" for s in seen)


def test_call_tool_raises_on_tool_error(monkeypatch) -> None:
    replies = iter([("s", b"{}"), ("s", b""), ("s", json.dumps(
        {"jsonrpc": "2.0", "id": 2, "result": {"isError": True,
                                               "content": [{"text": "boom"}]}}).encode())])
    monkeypatch.setattr(client, "_rpc", lambda *_a: next(replies))
    with pytest.raises(client.ToolError, match="boom"):
        client.call_tool(URL, "t", "room_inbox_wait", {})


def test_env_file_fills_only_missing_keys(tmp_path, monkeypatch) -> None:
    import room_env

    f = tmp_path / "room.env"
    f.write_text("# c\nROOM_MCP_TOKEN='from-file'\nVERA_MCP_URL=https://x.test/mcp\nbad line\n",
                 encoding="utf-8")
    monkeypatch.delenv("ROOM_MCP_TOKEN", raising=False)
    monkeypatch.setenv("VERA_MCP_URL", "https://env.test/mcp")
    room_env.load_env(f)
    assert room_env.require("ROOM_MCP_TOKEN") == "from-file"
    assert room_env.require("VERA_MCP_URL") == "https://env.test/mcp"
    monkeypatch.delenv("ROOM_MCP_TOKEN")
    room_env.load_env(tmp_path / "missing.env")
    with pytest.raises(SystemExit, match="ROOM_MCP_TOKEN"):
        room_env.require("ROOM_MCP_TOKEN")

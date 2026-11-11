from __future__ import annotations

import base64
import importlib.util
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest
from standardwebhooks import Webhook

SECRET = "whsec_" + base64.b64encode(bytes(range(32))).decode()
BODY = b'{"type":"room.message","room":"main"}'


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _listener() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    path = SCRIPTS / "room_push_listener.py"
    spec = importlib.util.spec_from_file_location("room_push_listener", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signed(now: datetime, body: bytes = BODY) -> dict[str, str]:
    msg_id = "room-msg-9"
    sig = Webhook(SECRET).sign(msg_id, now, body.decode())
    return {"webhook-id": msg_id, "webhook-timestamp": str(int(now.timestamp())),
            "webhook-signature": sig}


def test_verify_accepts_standard_webhooks_signature() -> None:
    mod = _listener()
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    assert mod.verify(SECRET, _signed(now), BODY, now.timestamp())


def test_verify_rejects_tampered_body_and_wrong_secret() -> None:
    mod = _listener()
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    headers = _signed(now)
    assert not mod.verify(SECRET, headers, BODY + b" ", now.timestamp())
    other = "whsec_" + base64.b64encode(bytes(range(1, 33))).decode()
    assert not mod.verify(other, headers, BODY, now.timestamp())


def test_verify_rejects_stale_timestamp_and_missing_headers() -> None:
    mod = _listener()
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    headers = _signed(now)
    assert not mod.verify(SECRET, headers, BODY, now.timestamp() + 3600)
    assert not mod.verify(SECRET, {}, BODY, now.timestamp())


def _payload(msg_id: int, body: str = "ping") -> dict[str, object]:
    return {"type": "room.message", "room": "main", "message": {
        "id": msg_id, "from": "dot", "to": "claude", "status": "info",
        "task_id": None, "body": body}}


def test_inbox_dedups_and_survives_restart(tmp_path: Path) -> None:
    _listener()
    from room_push_inbox import Inbox

    path = tmp_path / "inbox.jsonl"
    inbox = Inbox(path)
    assert inbox.add("room-msg-1", _payload(1)) is True
    assert inbox.add("room-msg-1", _payload(1)) is False
    assert [r["seq"] for r in inbox.wait_after(0, timeout=0.1)] == [1]
    assert inbox.wait_after(1, timeout=0.05) == []

    reopened = Inbox(path)
    assert reopened.latest == 1
    assert reopened.add("room-msg-1", _payload(1)) is False
    assert reopened.add("room-msg-2", _payload(2)) is True
    assert reopened.latest == 2


@pytest.fixture
def server(tmp_path: Path):
    mod = _listener()
    from room_push_inbox import Inbox

    inbox = Inbox(tmp_path / "inbox.jsonl")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(SECRET, inbox))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield mod, inbox, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_wait_blocks_until_message_then_prints_it(server, monkeypatch, capsys) -> None:
    mod, inbox, port = server
    monkeypatch.setenv("ROOM_PUSH_SECRET", SECRET)
    timer = threading.Timer(0.3, inbox.add, args=("room-msg-5", _payload(5, "привет")))
    timer.start()
    assert mod.wait(port, 0, 10.0) == 0
    out = capsys.readouterr().out
    assert "#5 dot -> claude [info]: привет" in out
    assert "latest=1" in out


def test_wait_times_out_without_messages(server, monkeypatch, capsys) -> None:
    mod, _, port = server
    monkeypatch.setenv("ROOM_PUSH_SECRET", SECRET)
    assert mod.wait(port, None, 0.2) == mod.EXIT_TIMEOUT
    assert "latest=0" in capsys.readouterr().out


def test_wait_endpoint_requires_secret(server) -> None:
    _, _, port = server
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/wait?timeout=0.1", timeout=5)
    assert err.value.code == 404

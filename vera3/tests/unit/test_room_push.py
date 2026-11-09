from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from standardwebhooks import Webhook
from vera_mcp import room_push
from vera_mcp.events_net import CallbackRejected

SECRET = "whsec_" + base64.b64encode(bytes(range(32))).decode()
URL = "https://claude.example.com/room"
MESSAGE = {"id": 7, "room": "main", "from": "dot", "to": "claude", "body": "привет",
           "message_id": "dot-1", "task_id": None, "in_reply_to": None, "status": "info",
           "session": None, "at": "2026-11-09T10:00:00"}


def test_parse_targets_accepts_valid_entries() -> None:
    targets = room_push.parse_targets(f"claude|{URL}|{SECRET}, dot|{URL}|{SECRET}")
    assert set(targets) == {"claude", "dot"}
    assert targets["claude"].url == URL


def test_parse_targets_empty_disables_push() -> None:
    assert room_push.parse_targets("") == {}


@pytest.mark.parametrize("raw", [
    f"claude|{URL}",
    f"claude|http://claude.example.com/room|{SECRET}",
    f"claude|{URL}|not-a-secret",
    f"claude|{URL}|{SECRET},claude|{URL}|{SECRET}",
])
def test_parse_targets_rejects_bad_entries(raw: str) -> None:
    with pytest.raises(ValueError):
        room_push.parse_targets(raw)


def test_payload_is_stable_json() -> None:
    body = room_push._payload(MESSAGE)
    assert json.loads(body) == {"type": "room.message", "room": "main", "message": MESSAGE}
    assert "привет" in body


def test_deliver_signs_body_and_stops_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[bytes, dict[str, str]]] = []

    async def fake_check(url: str) -> list[str]:
        return ["203.0.113.5"]

    async def fake_post(url: str, addresses: list[str], *, content: bytes,
                        headers: dict[str, str], timeout: float = 10.0) -> httpx.Response:
        sent.append((content, headers))
        return httpx.Response(204)

    monkeypatch.setattr(room_push, "check_callback_url", fake_check)
    monkeypatch.setattr(room_push, "post_pinned", fake_post)
    target = room_push.PushTarget(agent="claude", url=URL, secret=SECRET)
    asyncio.run(room_push._deliver(target, MESSAGE))

    assert len(sent) == 1
    content, headers = sent[0]
    lower = {k.lower(): v for k, v in headers.items()}
    assert lower["webhook-id"] == "room-msg-7"
    Webhook(SECRET).verify(content, lower)


def test_deliver_retries_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses = iter([500, 503, 200])
    calls: list[int] = []

    async def fake_check(url: str) -> list[str]:
        return ["203.0.113.5"]

    async def fake_post(url: str, addresses: list[str], *, content: bytes,
                        headers: dict[str, str], timeout: float = 10.0) -> httpx.Response:
        calls.append(1)
        return httpx.Response(next(statuses))

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(room_push, "check_callback_url", fake_check)
    monkeypatch.setattr(room_push, "post_pinned", fake_post)
    monkeypatch.setattr(room_push.asyncio, "sleep", no_sleep)
    target = room_push.PushTarget(agent="claude", url=URL, secret=SECRET)
    asyncio.run(room_push._deliver(target, MESSAGE))
    assert len(calls) == 3


def test_deliver_gives_up_after_attempts_and_does_not_raise(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_check(url: str) -> list[str]:
        return ["203.0.113.5"]

    async def fake_post(url: str, addresses: list[str], *, content: bytes,
                        headers: dict[str, str], timeout: float = 10.0) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused")

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(room_push, "check_callback_url", fake_check)
    monkeypatch.setattr(room_push, "post_pinned", fake_post)
    monkeypatch.setattr(room_push.asyncio, "sleep", no_sleep)
    target = room_push.PushTarget(agent="claude", url=URL, secret=SECRET)
    asyncio.run(room_push._deliver(target, MESSAGE))
    assert len(calls) == room_push.PUSH_ATTEMPTS


def test_deliver_does_not_retry_rejected_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_check(url: str) -> list[str]:
        calls.append(1)
        raise CallbackRejected("address_not_public")

    monkeypatch.setattr(room_push, "check_callback_url", fake_check)
    target = room_push.PushTarget(agent="claude", url=URL, secret=SECRET)
    asyncio.run(room_push._deliver(target, MESSAGE))
    assert len(calls) == 1


def test_schedule_push_skips_unknown_recipient_and_self(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(room_push, "_cache", {
        "claude": room_push.PushTarget(agent="claude", url=URL, secret=SECRET)})
    scheduled: list[object] = []

    async def record(target: object, message: object) -> None:
        scheduled.append(target)

    monkeypatch.setattr(room_push, "_deliver", record)

    async def run() -> None:
        room_push.schedule_push({**MESSAGE, "to": None})
        room_push.schedule_push({**MESSAGE, "to": "codex"})
        room_push.schedule_push({**MESSAGE, "from": "claude"})
        room_push.schedule_push(MESSAGE)
        await asyncio.gather(*room_push._pending)

    asyncio.run(run())
    assert len(scheduled) == 1

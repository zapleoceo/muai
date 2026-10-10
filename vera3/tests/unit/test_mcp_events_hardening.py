"""MCP Events: ревью безопасности (TTL, rebinding, NAT64, квоты, конкурентность, пустые имена)."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from mcp.shared.exceptions import MCPError
from mcp_events_support import (
    HOST,
    URL,
    FakeReceiver,
    new_secret,
    open_client,
    rpc,
    subscribe_params,
)
from sqlalchemy import select
from vera_mcp import events_deliver, events_net
from vera_mcp.events_deliver import deliver_due
from vera_mcp.events_net import CallbackRejected, check_callback_url
from vera_mcp.events_subscribe import (
    DEFAULT_TTL_MS,
    MAX_SUBSCRIPTIONS_PER_PRINCIPAL,
    MAX_TTL_MS,
    canonical_arguments,
)
from vera_shared.db.engine import get_session
from vera_shared.db.models_room_events import (
    RoomEventOutboxRow,
    RoomEventSubscriptionRow,
)
from vera_shared.room import tasks
from vera_shared.room.event_outbox import prune_outbox
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
PUBLIC = "93.184.216.34"


@pytest.fixture
def receiver(monkeypatch, sqlite_db):
    monkeypatch.setenv("ROOM_TOKENS", "claude:room-claude-0123456789-0123456789")
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    fake = FakeReceiver(secret=new_secret())
    monkeypatch.setattr(events_net.net, "transport", httpx.ASGITransport(app=fake))

    async def resolve(_host: str) -> list[str]:
        return [PUBLIC]

    monkeypatch.setattr(events_net.net, "resolver", resolve)
    return fake


async def subscribe(fake: FakeReceiver, **params) -> dict:
    p = subscribe_params(fake.secret)
    p.update(params)
    async with open_client() as c:
        return await rpc(c, "claude", "events/subscribe", p)


async def only_sub() -> RoomEventSubscriptionRow:
    async with get_session() as s:
        return (await s.execute(select(RoomEventSubscriptionRow))).scalars().one()


async def open_task(task_id: str = "T1") -> None:
    await tasks.open_task(room="main", task_id=task_id, agent="codex", title=None, paths=None)


@pytest.mark.parametrize(("extra", "expected_ms"), [
    ({"ttlMs": None}, DEFAULT_TTL_MS),
    ({}, DEFAULT_TTL_MS),
    ({"ttlMs": 10**15}, MAX_TTL_MS),
])
async def test_ttl_is_always_bounded(receiver, extra, expected_ms):
    res = await subscribe(receiver, **extra)
    assert "error" not in res and res["result"]["refreshBefore"]
    left = (await only_sub()).expires_at - utc_naive_now()
    assert timedelta(milliseconds=expected_ms) - timedelta(seconds=10) <= left
    assert left <= timedelta(milliseconds=expected_ms)


async def test_delivery_revokes_when_token_removed(receiver, monkeypatch):
    await subscribe(receiver)
    await open_task()
    monkeypatch.setenv("ROOM_TOKENS", "other:room-other-0123456789-0123456789")
    await deliver_due()
    assert receiver.events == []
    assert (await only_sub()).status == "revoked"
    async with get_session() as s:
        assert {r.status for r in (await s.execute(select(RoomEventOutboxRow))).scalars()} == {"revoked"}


async def test_delivery_revokes_when_room_no_longer_allowed(receiver, monkeypatch):
    await subscribe(receiver)
    await open_task()
    monkeypatch.setenv("ROOM_NAMES", "elsewhere")
    await deliver_due()
    assert receiver.events == [] and (await only_sub()).status == "revoked"


async def test_rebinding_connects_to_validated_ip_once(receiver, monkeypatch):
    answers = iter([[PUBLIC], ["10.0.0.5"], ["10.0.0.5"]])
    calls: list[str] = []
    seen: list[httpx.Request] = []

    async def rebinding(host: str) -> list[str]:
        calls.append(host)
        return next(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"challenge": json.loads(request.content).get("challenge")})

    monkeypatch.setattr(events_net.net, "resolver", rebinding)
    monkeypatch.setattr(events_net.net, "transport", httpx.MockTransport(handler))
    res = await subscribe(receiver)
    assert "error" not in res and calls == [HOST]
    (req,) = seen
    assert req.url.host == PUBLIC and req.headers["host"] == HOST
    assert req.extensions["sni_hostname"] == HOST


async def test_delivery_is_pinned_too(receiver, monkeypatch):
    await subscribe(receiver)
    await open_task()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(events_net.net, "transport", httpx.MockTransport(handler))
    await deliver_due()
    (req,) = seen
    assert req.url.host == PUBLIC and req.headers["host"] == HOST and req.extensions["sni_hostname"] == HOST


@pytest.mark.parametrize("addr", [
    "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b:1::1", "2002:7f00:1::1", "2002:a00:1::",
    "::10.0.0.1", "::8.8.8.8", "2001:0:4136:e378:8000:63bf:3fff:fdd2",
])
async def test_embedded_private_ipv6_is_not_public(addr):
    assert not events_net._is_public(addr)


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946", "64:ff9b::5db8:d822"])
async def test_public_addresses_stay_public(addr):
    assert events_net._is_public(addr)


@pytest.mark.parametrize("url", ["https://" + "a" * 70 + ".example.test/cb", "https://a..b.test/cb"])
async def test_bad_hostnames_are_rejected_not_crashing(url):
    with pytest.raises(CallbackRejected) as exc:
        await check_callback_url(url)
    assert exc.value.reason == "bad_url"


async def test_quota_per_principal(receiver):
    async with get_session() as s:
        for i in range(MAX_SUBSCRIPTIONS_PER_PRINCIPAL):
            s.add(RoomEventSubscriptionRow(
                id=f"sub_x{i}", principal="claude", room="main", event_name="ready", kinds=[],
                callback_url=URL, encrypted_secret="x", status="active",
                expires_at=utc_naive_now() + timedelta(days=1)))
    res = await subscribe(receiver)
    assert res["error"]["code"] == -32602 and res["error"]["data"]["reason"] == "too_many_subscriptions"


async def test_prune_outbox_only_old_terminal_rows(receiver):
    await subscribe(receiver)
    await open_task("A")
    await open_task("B")
    old = utc_naive_now() - timedelta(days=40)
    async with get_session() as s:
        rows = list((await s.execute(select(RoomEventOutboxRow).order_by(RoomEventOutboxRow.id))).scalars())
        rows[0].status, rows[0].created_at = "delivered", old
        rows[1].created_at = old
    async with get_session() as s:
        assert await prune_outbox(s, older_than_days=30) == 1
    async with get_session() as s:
        left = list((await s.execute(select(RoomEventOutboxRow))).scalars())
        assert [r.status for r in left] == ["pending"]


async def test_slow_endpoint_does_not_stall_others(receiver, monkeypatch):
    for n in range(8):
        await subscribe(receiver, arguments={"room": "main", "task_id": f"T{n}"})
        await open_task(f"T{n}")
    done: list[int] = []
    running = peak = 0

    async def fake_attempt(row_id: int, _now) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(10 if row_id == 1 else 0.01)
            done.append(row_id)
        finally:
            running -= 1

    monkeypatch.setattr(events_deliver, "_attempt", fake_attempt)
    monkeypatch.setattr(events_deliver, "ATTEMPT_DEADLINE_S", 0.3)
    assert await deliver_due() == 8
    assert 1 not in done and len(done) == 7
    assert peak == events_deliver.MAX_CONCURRENCY


async def test_task_id_length_is_capped(monkeypatch):
    monkeypatch.setenv("ROOM_NAMES", "main")
    with pytest.raises(MCPError) as exc:
        canonical_arguments({"room": "main", "task_id": "t" * 129})
    assert exc.value.error.data["reason"] == "bad_arguments"
    assert canonical_arguments({"room": "main", "task_id": "t" * 128})["task_id"] == "t" * 128


async def test_http_client_ignores_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    async with events_net.http_client() as client:
        assert client._mounts == {}

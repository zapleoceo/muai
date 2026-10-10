"""MCP Events: подписка (challenge, id, ACL, SSRF) и outbox (атомарность, самопетля)."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from mcp_events_support import (
    HOST,
    FakeReceiver,
    new_secret,
    open_client,
    rpc,
    subscribe_params,
)
from sqlalchemy import func, select
from vera_mcp import events_net
from vera_shared.db.engine import get_session
from vera_shared.db.models_room_events import (
    RoomEventOutboxRow,
    RoomEventSubscriptionRow,
)
from vera_shared.room import tasks
from vera_shared.room.task_events import record_event

pytestmark = pytest.mark.asyncio


@pytest.fixture
def secret() -> str:
    return new_secret()


@pytest.fixture
def receiver(secret, monkeypatch, sqlite_db):
    monkeypatch.setenv("MCP_TOKENS", "claude:owner-token-0123456789-0123456789")
    monkeypatch.setenv("ROOM_TOKENS",
                       "claude:room-claude-0123456789-0123456789,codex:room-codex-abcdefghij-abcdefghij")
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    monkeypatch.setenv("ROOM_NAMES", "main,other")
    fake = FakeReceiver(secret=secret)

    async def resolve(host: str) -> list[str]:
        return {HOST: ["93.184.216.34"], "internal.example.test": ["10.0.0.5"]}[host]

    monkeypatch.setattr(events_net.net, "resolver", resolve)
    monkeypatch.setattr(events_net.net, "transport", httpx.ASGITransport(app=fake))
    return fake


async def count(model) -> int:
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def subscribe(who: str, secret: str, **kw):
    async with open_client() as c:
        return await rpc(c, who, "events/subscribe", subscribe_params(secret, **kw))


async def test_c3_challenge_ok_stores_encrypted_secret(receiver, secret):
    res = (await subscribe("claude", secret))["result"]
    assert res["id"].startswith("sub_") and res["refreshBefore"].endswith("Z")
    assert [r["verified"] for r in receiver.requests] == [True]
    async with get_session() as s:
        row = await s.get(RoomEventSubscriptionRow, res["id"])
    assert row.principal == "claude" and secret not in row.encrypted_secret
    assert receiver.requests[0]["headers"]["x-mcp-subscription-id"] == res["id"]


async def test_c3_id_is_deterministic_and_scoped(receiver, secret):
    a = (await subscribe("claude", secret))["result"]["id"]
    again = (await subscribe("claude", secret))["result"]["id"]
    other_args = (await subscribe("claude", secret, task_id="T1"))["result"]["id"]
    other_principal = (await subscribe("codex", secret))["result"]["id"]
    assert a == again and len({a, other_args, other_principal}) == 3
    assert await count(RoomEventSubscriptionRow) == 3


@pytest.mark.parametrize("mode", ["wrong", "redirect"])
async def test_c3_challenge_failure_is_32015_and_stores_nothing(receiver, secret, mode):
    receiver.mode = mode
    err = (await subscribe("claude", secret))["error"]
    assert err["code"] == -32015 and err["data"]["reason"] == "challenge_failed"
    assert await count(RoomEventSubscriptionRow) == 0


async def test_c4_unsubscribe_is_scoped_to_principal(receiver, secret):
    sub = (await subscribe("claude", secret))["result"]["id"]
    async with open_client() as c:
        params = subscribe_params(secret)
        del params["delivery"]["secret"]
        await rpc(c, "codex", "events/unsubscribe", params)
        async with get_session() as s:
            assert (await s.get(RoomEventSubscriptionRow, sub)).status == "active"
        assert (await rpc(c, "claude", "events/unsubscribe", params))["result"]["resultType"] == "complete"
    async with get_session() as s:
        assert (await s.get(RoomEventSubscriptionRow, sub)).status == "revoked"


async def test_c4_room_outside_rights_is_rejected(receiver, secret, monkeypatch):
    monkeypatch.setenv("ROOM_NAMES", "main")
    err = (await subscribe("claude", secret, room="other"))["error"]
    assert err["code"] == -32602 and err["data"]["reason"] == "room_not_allowed"


@pytest.mark.parametrize(("url", "reason"), [
    ("http://hooks.example.test/cb", "https_required"),
    ("https://127.0.0.1/cb", "address_not_public"),
    ("https://10.1.2.3/cb", "address_not_public"),
    ("https://169.254.169.254/latest", "address_not_public"),
    ("https://[::1]/cb", "address_not_public"),
    ("https://[::ffff:127.0.0.1]/cb", "address_not_public"),
    ("https://internal.example.test/cb", "address_not_public"),
    ("https://user:pw@hooks.example.test/cb", "credentials_in_url"),
])
async def test_c11_ssrf_cases_never_reach_the_network(receiver, secret, url, reason, caplog):
    caplog.set_level(logging.DEBUG)
    err = (await subscribe("claude", secret, url=url))["error"]
    assert err["code"] == -32602 and err["data"]["reason"] == reason
    assert receiver.requests == []
    assert secret not in caplog.text and secret[6:] not in caplog.text


async def test_c11_secret_never_logged_on_success(receiver, secret, caplog):
    caplog.set_level(logging.DEBUG)
    await subscribe("claude", secret)
    assert secret not in caplog.text and secret[6:] not in caplog.text


async def test_c11_weak_secret_rejected(receiver):
    err = (await subscribe("claude", "whsec_" + "QQ=="))["error"]
    assert err["data"]["reason"] == "bad_secret" and receiver.requests == []


async def active_sub(secret: str, principal: str = "claude", **kw) -> str:
    res = await subscribe(principal, secret, **kw)
    return res["result"]["id"]


async def test_outbox_row_written_with_envelope(receiver, secret):
    sub = await active_sub(secret)
    await tasks.open_task(room="main", task_id="T1", agent="codex", title="t", paths=None)
    async with get_session() as s:
        rows = (await s.execute(select(RoomEventOutboxRow))).scalars().all()
    assert [(r.subscription_id, r.event_name, r.status) for r in rows] == [(sub, "ready", "pending")]
    data = json.loads(rows[0].body)["data"]
    assert data["task_id"] == "T1" and data["room"] == "main" and data["kind"] == "created"
    assert data["actor"] == "codex" and data["correlation_id"] == "main/T1"
    assert data["causation_id"] is None and data["revision"] >= 1


async def test_c5_rollback_removes_outbox(receiver, secret):
    await active_sub(secret)
    with pytest.raises(RuntimeError):
        async with get_session() as s:
            await record_event(s, room="main", task_id="T9", kind="created", agent="codex")
            raise RuntimeError("boom")
    assert await count(RoomEventOutboxRow) == 0


async def test_filters_by_task_and_kind(receiver, secret):
    await active_sub(secret, task_id="T2", kind=["created"])
    await tasks.open_task(room="main", task_id="T1", agent="codex", title=None, paths=None)
    await tasks.open_task(room="main", task_id="T2", agent="codex", title=None, paths=None)
    async with get_session() as s:
        assert [r.task_id for r in (await s.execute(select(RoomEventOutboxRow))).scalars()] == ["T2"]


async def outbox_names() -> list[tuple[str, str]]:
    async with get_session() as s:
        rows = (await s.execute(select(RoomEventOutboxRow).order_by(RoomEventOutboxRow.id))).scalars()
        return [(r.subscription_id, r.event_name) for r in rows]


async def test_c12_reaction_to_own_wake_is_suppressed(receiver, secret):
    ready = await active_sub(secret, principal="claude", task_id="T1")
    await tasks.open_task(room="main", task_id="T1", agent="codex", title=None, paths=None)
    assert await outbox_names() == [(ready, "ready")]
    async with get_session() as s:
        await record_event(s, room="main", task_id="T1", kind="released", agent="claude")
    assert await outbox_names() == [(ready, "ready")]


async def test_c12_result_and_blocker_from_same_principal_are_delivered(receiver, secret):
    ready = await active_sub(secret, principal="claude", task_id="T1")
    result = await active_sub(secret, principal="claude", task_id="T1", name="result")
    blocker = await active_sub(secret, principal="claude", task_id="T1", name="blocker")
    await tasks.open_task(room="main", task_id="T1", agent="codex", title=None, paths=None)
    async with get_session() as s:
        await record_event(s, room="main", task_id="T1", kind="blocked", agent="claude")
        await record_event(s, room="main", task_id="T1", kind="done", agent="claude")
    assert await outbox_names() == [(ready, "ready"), (blocker, "blocker"), (result, "result")]


async def test_c12_other_actors_events_are_not_suppressed(receiver, secret):
    ready = await active_sub(secret, principal="claude", task_id="T1")
    await tasks.open_task(room="main", task_id="T1", agent="codex", title=None, paths=None)
    async with get_session() as s:
        await record_event(s, room="main", task_id="T1", kind="released", agent="codex")
    assert await outbox_names() == [(ready, "ready"), (ready, "ready")]

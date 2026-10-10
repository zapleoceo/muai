"""MCP Events: доставка из outbox (повторы, 410/413, рестарт, отзыв, подпись на попытку)."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mcp_events_support import (
    FakeReceiver,
    new_secret,
    open_client,
    rpc,
    subscribe_params,
)
from sqlalchemy import select
from vera_mcp import events_deliver, events_net
from vera_mcp.events_catalog import is_stale_event
from vera_mcp.events_deliver import MAX_ATTEMPTS, backoff_delay, deliver_due
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow
from vera_shared.db.models_room_events import (
    RoomEventOutboxRow,
    RoomEventSubscriptionRow,
)
from vera_shared.room import tasks
from vera_shared.room.event_outbox import enqueue_task_event
from vera_shared.room.task_events import record_event
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
LATER = timedelta(hours=2)


@pytest.fixture
def receiver(monkeypatch, sqlite_db):
    monkeypatch.setenv("ROOM_TOKENS", "claude:room-claude-0123456789-0123456789")
    monkeypatch.setenv("MCP_TOKENS", "owner:owner-token-0123456789-0123456789")
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    fake = FakeReceiver(secret=new_secret())

    async def resolve(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(events_net.net, "resolver", resolve)
    monkeypatch.setattr(events_net.net, "transport", httpx.ASGITransport(app=fake))
    return fake


async def subscribed(fake: FakeReceiver, **kw) -> str:
    async with open_client() as c:
        res = await rpc(c, "claude", "events/subscribe", subscribe_params(fake.secret, **kw))
    return res["result"]["id"]


async def new_task(task_id: str = "T1") -> None:
    await tasks.open_task(room="main", task_id=task_id, agent="codex", title=None, paths=None)


async def rows() -> list[RoomEventOutboxRow]:
    async with get_session() as s:
        return list((await s.execute(select(RoomEventOutboxRow).order_by(RoomEventOutboxRow.id))).scalars())


async def test_delivery_is_signed_with_event_id_and_subscription_header(receiver):
    sub = await subscribed(receiver)
    await new_task()
    assert await deliver_due() == 1
    (req,) = receiver.events
    row = (await rows())[0]
    assert req["verified"] and req["headers"]["webhook-id"] == row.event_id
    assert req["headers"]["x-mcp-subscription-id"] == sub
    assert req["json"]["name"] == "ready" and req["json"]["data"]["task_id"] == "T1"
    assert row.status == "delivered"


async def test_c6_unique_event_per_subscription(receiver):
    await subscribed(receiver)
    await new_task()
    async with get_session() as s:
        event = (await s.execute(select(RoomTaskEventRow))).scalars().first()
        assert await enqueue_task_event(s, event) == []
    assert len(await rows()) == 1


async def test_c6_retry_keeps_webhook_id_with_fresh_valid_signature(receiver, monkeypatch):
    receiver.statuses = [500, 200]
    ticks = iter(datetime(2026, 10, 1, 12, 0, n, tzinfo=UTC) for n in range(1, 10))
    real = events_deliver.signed_headers
    monkeypatch.setattr(events_deliver, "signed_headers",
                        lambda *a: real(*a, now=next(ticks)))
    await subscribed(receiver)
    await new_task()
    await deliver_due()
    await deliver_due(now=utc_naive_now() + LATER)
    first, second = (r["headers"] for r in receiver.events)
    assert first["webhook-id"] == second["webhook-id"]
    assert first["webhook-signature"] != second["webhook-signature"]
    assert first["webhook-timestamp"] != second["webhook-timestamp"]


async def test_c7_500_is_retried_with_backoff_then_delivered(receiver):
    receiver.statuses = [500, 200]
    await subscribed(receiver)
    await new_task()
    assert await deliver_due() == 1
    row = (await rows())[0]
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "http_500")
    assert await deliver_due() == 0
    assert await deliver_due(now=utc_naive_now() + LATER) == 1
    assert (await rows())[0].status == "delivered"


async def test_c7_gives_up_after_max_attempts(receiver):
    receiver.statuses = [500]
    await subscribed(receiver)
    await new_task()
    for n in range(1, MAX_ATTEMPTS + 3):
        await deliver_due(now=utc_naive_now() + LATER * n)
    row = (await rows())[0]
    assert (row.status, row.attempts) == ("failed", MAX_ATTEMPTS)
    assert len(receiver.events) == MAX_ATTEMPTS


async def test_c7_410_revokes_subscription_without_retry(receiver):
    receiver.statuses = [410]
    sub = await subscribed(receiver)
    await new_task("T1")
    await new_task("T2")
    await deliver_due()
    async with get_session() as s:
        assert (await s.get(RoomEventSubscriptionRow, sub)).status == "revoked"
    assert {r.status for r in await rows()} == {"revoked"}
    await deliver_due(now=utc_naive_now() + LATER)
    assert len(receiver.events) == 1


async def test_c7_413_drops_event_without_retry(receiver):
    receiver.statuses = [413, 200]
    await subscribed(receiver)
    await new_task("T1")
    await new_task("T2")
    await deliver_due()
    await deliver_due(now=utc_naive_now() + LATER)
    assert [(r.task_id, r.status) for r in await rows()] == [("T1", "dropped"), ("T2", "delivered")]
    assert len(receiver.events) == 2


async def test_c8_restart_resumes_only_pending(receiver):
    receiver.statuses = [200, 500, 200]
    await subscribed(receiver)
    await new_task("T1")
    await new_task("T2")
    await deliver_due()
    assert [r.status for r in await rows()] == ["delivered", "pending"]
    await deliver_due(now=utc_naive_now() + LATER)
    assert [r.status for r in await rows()] == ["delivered", "delivered"]
    assert [r["json"]["data"]["task_id"] for r in receiver.events] == ["T1", "T2", "T2"]


async def test_c9_revision_grows_and_stale_events_are_detected(receiver):
    await subscribed(receiver, task_id="T1")
    await new_task()
    async with get_session() as s:
        await record_event(s, room="main", task_id="T1", kind="released", agent="codex")
    revisions = [json.loads(r.body)["data"]["revision"] for r in await rows()]
    assert revisions == sorted(revisions) and len(set(revisions)) == 2
    assert is_stale_event(revisions[0], revisions[1])
    assert not is_stale_event(revisions[1], revisions[1])


async def test_c10_revoked_subscription_cancels_pending(receiver):
    receiver.statuses = [200]
    sub = await subscribed(receiver)
    await new_task()
    async with open_client() as c:
        params = subscribe_params(receiver.secret)
        del params["delivery"]["secret"]
        await rpc(c, "claude", "events/unsubscribe", params)
    assert await deliver_due() == 0
    assert (await rows())[0].status == "revoked" and receiver.events == []
    assert sub


async def test_c10_expired_subscription_is_not_delivered(receiver):
    sub = await subscribed(receiver)
    await new_task()
    async with get_session() as s:
        (await s.get(RoomEventSubscriptionRow, sub)).expires_at = utc_naive_now() - timedelta(seconds=1)
    assert await deliver_due() == 1
    assert (await rows())[0].status == "revoked" and receiver.events == []


async def test_delivery_rechecks_address_and_never_logs_secret(receiver, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    await subscribed(receiver)
    await new_task()

    async def rebound(_host: str) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(events_net.net, "resolver", rebound)
    await deliver_due()
    assert (await rows())[0].last_error == "address_not_public" and receiver.events == []
    assert receiver.secret not in caplog.text and receiver.secret[6:] not in caplog.text


def test_backoff_is_bounded_and_jittered():
    delays = [backoff_delay(n, rand=lambda: 0.0) for n in range(1, 12)]
    assert delays == sorted(delays) and max(delays) <= events_deliver.MAX_DELAY_S
    assert backoff_delay(3, rand=lambda: 1.0) == 2 * backoff_delay(3, rand=lambda: 0.0)

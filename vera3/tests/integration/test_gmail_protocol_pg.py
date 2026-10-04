"""Provider-shaped, offline Gmail pagination against real PostgreSQL migrations."""

from __future__ import annotations

import base64
import os

import pytest
from shadow_pg_support import isolated_migrated_shadow_schema, use_schema
from sqlalchemy import text
from vera_shared.ingest import gmail_protocol as protocol
from vera_shared.ingest.gmail_pilot import pilot_status, register_synthetic_mailbox
from vera_shared.ingest.shadow_types import Quarantine

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)
SUB = "synthetic-protocol-sub"
AT = "2026-10-04T01:00:00Z"
BIG = 18446744073709551620
WHOLE_SCOPE = {"labelIds": [], "includeSpamTrash": True, "q": ""}
WINDOW_START = "0001-01-01T00:00:00Z"
WINDOW_END = "9999-12-31T00:00:00Z"


def history(position: str, mid: str, field: str = "messagesAdded") -> dict:
    item: dict = {"message": {"id": mid, "threadId": f"thread-{mid}"}}
    if field.startswith("labels"):
        item["labelIds"] = ["STARRED"]
    return {"id": position, field: [item]}


def get_message(mid: str, version: str) -> dict:
    encoded = base64.urlsafe_b64encode(f"body-{mid}".encode()).decode().rstrip("=")
    return {
        "id": mid,
        "threadId": f"thread-{mid}",
        "historyId": version,
        "internalDate": "1791072000000",  # 2026-10-04 UTC
        "labelIds": ["INBOX"],
        "payload": {"mimeType": "text/plain", "body": {"data": encoded}},
    }


async def setup(conn, schema):
    await use_schema(conn, schema)
    await register_synthetic_mailbox(conn, SUB, "offline@example.test", str(BIG))


@pytest.mark.asyncio
async def test_continuation_replay_overlap_and_terminal_highwater(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    first = {
        "historyId": str(BIG + 900),
        "nextPageToken": "page-two",
        "history": [
            history(str(BIG + 5), "a"),
            history(str(BIG + 5), "b", "labelsAdded"),
        ],
    }
    second = {
        "historyId": str(BIG + 950),
        "history": [history(str(BIG + 5), "a"), history(str(BIG + 11), "c")],
    }
    http = protocol.SyntheticGmailHttp(
        {
            ("history.list", ""): (200, first),
            ("history.list", "page-two"): (200, second),
        }
    )
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            assert (
                await protocol.ingest_synthetic_history_page(
                    conn, SUB, str(BIG), None, http
                )
                == "page-two"
            )
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG)
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_gmail_protocol_obligations")
                )
            ).scalar_one() == 2
            await conn.rollback()
            # Lost acknowledgement replays the same first response exactly.
            assert (
                await protocol.ingest_synthetic_history_page(
                    conn, SUB, str(BIG), None, http
                )
                == "page-two"
            )
            assert (
                await protocol.ingest_synthetic_history_page(
                    conn, SUB, str(BIG), "page-two", http
                )
                is None
            )
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG)
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_gmail_protocol_obligations")
                )
            ).scalar_one() == 3
            await conn.rollback()
            assert await protocol.finish_history_chain(conn, SUB)
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG + 950)
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_gmail_pilot_events")
                )
            ).scalar_one() == 3
            await conn.rollback()
            assert not await protocol.finish_history_chain(conn, SUB)


@pytest.mark.asyncio
async def test_terminal_capture_failure_retries_without_early_cursor(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    response = {"historyId": str(BIG + 40), "history": [history(str(BIG + 7), "a")]}
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await protocol.capture_history_response(
                conn,
                SUB,
                start_history_id=str(BIG),
                requested_token=None,
                response=response,
            )
            original = protocol.capture_history_page

            async def crash(*args, **kwargs):
                raise RuntimeError("crash before cursor advance")

            monkeypatch.setattr(protocol, "capture_history_page", crash)
            with pytest.raises(RuntimeError, match="before cursor advance"):
                await protocol.finish_history_chain(conn, SUB)
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG)
            monkeypatch.setattr(protocol, "capture_history_page", original)
            assert await protocol.finish_history_chain(conn, SUB)
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG + 40)


@pytest.mark.asyncio
async def test_ambiguous_same_position_and_changed_replay_hold_cursor(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    ambiguous = {
        "historyId": str(BIG + 20),
        "history": [
            {
                "id": str(BIG + 5),
                "messagesAdded": [{"message": {"id": "a"}}],
                "labelsAdded": [{"message": {"id": "a"}, "labelIds": ["X"]}],
            }
        ],
    }
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            with pytest.raises(Quarantine, match="ambiguous same-position"):
                await protocol.capture_history_response(
                    conn,
                    SUB,
                    start_history_id=str(BIG),
                    requested_token=None,
                    response=ambiguous,
                )
            safe = {"historyId": str(BIG + 20), "nextPageToken": "next", "history": []}
            await protocol.capture_history_response(
                conn,
                SUB,
                start_history_id=str(BIG),
                requested_token=None,
                response=safe,
            )
            with pytest.raises(Quarantine, match="changed on replay"):
                await protocol.capture_history_response(
                    conn,
                    SUB,
                    start_history_id=str(BIG),
                    requested_token=None,
                    response={**safe, "historyId": str(BIG + 21)},
                )
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG)


@pytest.mark.asyncio
async def test_history_404_full_sync_pages_and_bridge(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            page = {"historyId": str(BIG + 20), "nextPageToken": "lost", "history": []}
            await protocol.capture_history_response(
                conn,
                SUB,
                start_history_id=str(BIG),
                requested_token=None,
                response=page,
            )
            http = protocol.SyntheticGmailHttp({("history.list", "lost"): (404, {})})
            await protocol.ingest_synthetic_history_page(
                conn, SUB, str(BIG), "lost", http
            )
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG)
            assert (
                await conn.execute(
                    text("SELECT state FROM brain_gmail_protocol_history")
                )
            ).scalar_one() == "expired"
            await conn.rollback()
            await protocol.start_full_sync(
                conn,
                SUB,
                scope=WHOLE_SCOPE,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
            first = {
                "messages": [{"id": "a", "threadId": "ta"}],
                "nextPageToken": "list-next",
                "resultSizeEstimate": 2,
            }
            assert (
                await protocol.capture_full_sync_response(
                    conn,
                    SUB,
                    requested_token=None,
                    response=first,
                    fetched={"a": get_message("a", str(BIG + 500))},
                )
                == "list-next"
            )
            with pytest.raises(Quarantine, match="enumeration is incomplete"):
                await protocol.finish_full_sync(conn, SUB, AT)
            second = {
                "messages": [{"id": "b", "threadId": "tb"}],
                "resultSizeEstimate": 2,
            }
            assert (
                await protocol.capture_full_sync_response(
                    conn,
                    SUB,
                    requested_token="list-next",
                    response=second,
                    fetched={"b": get_message("b", str(BIG + 450))},
                )
                is None
            )
            anchor = await protocol.finish_full_sync(conn, SUB, AT)
            assert anchor == str(BIG + 500)
            assert await protocol.full_sync_state(conn, SUB) == "bridging"
            assert (await pilot_status(conn, SUB))["coverage_break"] is True
            bridge = {"historyId": str(BIG + 550), "history": []}
            await protocol.capture_history_response(
                conn,
                SUB,
                start_history_id=anchor,
                requested_token=None,
                response=bridge,
            )
            assert await protocol.finish_history_chain(conn, SUB)
            assert await protocol.full_sync_state(conn, SUB) == "complete"
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG + 550)
            assert (await pilot_status(conn, SUB))["coverage_break"] is True


@pytest.mark.asyncio
async def test_full_sync_get_failure_and_enumeration_race_are_unresolved(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await protocol.expire_history_chain(conn, SUB)
            with pytest.raises(Quarantine, match="whole-mailbox"):
                await protocol.start_full_sync(
                    conn,
                    SUB,
                    scope={"labelIds": ["INBOX"], "includeSpamTrash": False, "q": ""},
                    window_start="2026-10-03T00:00:00Z",
                    window_end="2026-10-05T00:00:00Z",
                )
            await protocol.start_full_sync(
                conn,
                SUB,
                scope=WHOLE_SCOPE,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
            first = {"messages": [{"id": "a"}], "nextPageToken": "p2"}
            with pytest.raises(Quarantine, match="incomplete or duplicated"):
                await protocol.capture_full_sync_response(
                    conn, SUB, requested_token=None, response=first, fetched={}
                )
            with pytest.raises(Quarantine, match="identity differs"):
                await protocol.capture_full_sync_response(
                    conn,
                    SUB,
                    requested_token=None,
                    response=first,
                    fetched={"a": get_message("different", str(BIG + 500))},
                )
            await protocol.capture_full_sync_response(
                conn,
                SUB,
                requested_token=None,
                response=first,
                fetched={"a": get_message("a", str(BIG + 500))},
            )
            with pytest.raises(Quarantine, match="raced beyond"):
                await protocol.capture_full_sync_response(
                    conn,
                    SUB,
                    requested_token="p2",
                    response={"messages": [{"id": "b"}]},
                    fetched={"b": get_message("b", str(BIG + 501))},
                )
            assert await protocol.full_sync_state(conn, SUB) == "paging"
            assert (await pilot_status(conn, SUB))["captured_cursor"] == str(BIG)


@pytest.mark.asyncio
async def test_full_sync_bridge_race_and_publish_retry(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await protocol.expire_history_chain(conn, SUB)
            await protocol.start_full_sync(
                conn,
                SUB,
                scope=WHOLE_SCOPE,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
            await protocol.capture_full_sync_response(
                conn,
                SUB,
                requested_token=None,
                response={"messages": [{"id": "a"}]},
                fetched={"a": get_message("a", str(BIG + 500))},
            )
            original = protocol.capture_present_resync

            async def bridge_during_publish(*args, **kwargs):
                await original(*args, **kwargs)
                await protocol.capture_history_response(
                    conn,
                    SUB,
                    start_history_id=str(BIG + 500),
                    requested_token=None,
                    response={"historyId": str(BIG + 510), "history": []},
                )
                await protocol.finish_history_chain(conn, SUB)

            monkeypatch.setattr(
                protocol, "capture_present_resync", bridge_during_publish
            )
            await protocol.finish_full_sync(conn, SUB, AT)
            assert await protocol.full_sync_state(conn, SUB) == "complete"
            assert await protocol.finish_full_sync(conn, SUB, AT) == str(BIG + 500)

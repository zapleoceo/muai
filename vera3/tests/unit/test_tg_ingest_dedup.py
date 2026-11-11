"""ingestor_telegram: живой юзербот и бэкфилл пишут через общий insert_events.

Раньше оба делали check-then-insert (SELECT, потом add) — та же гонка, от
которой шлюз лечили через ON CONFLICT. Live и backfill ходят параллельно по
одним и тем же сообщениям, поэтому дедуп обязан быть атомарным.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test")
os.environ.setdefault("TELEGRAM_PHONE", "+10000000000")

from sqlalchemy import func, select  # noqa: E402
from vera_shared.db.models import EventRow  # noqa: E402

DATE = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


class User(SimpleNamespace):
    pass


def _msg(msg_id: int, chat, sender, text: str = "привет"):
    return SimpleNamespace(
        id=msg_id, message=text, text=text, date=DATE,
        get_chat=AsyncMock(return_value=chat),
        get_sender=AsyncMock(return_value=sender),
    )


async def _rows() -> list[EventRow]:
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        return list((await s.execute(select(EventRow))).scalars())


async def _count() -> int:
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(EventRow))).scalar_one()


@pytest.mark.usefixtures("sqlite_db")
@pytest.mark.asyncio
async def test_userbot_repeat_message_inserted_once_and_graph_synced_once():
    from ingestor_telegram import userbot

    me = User(id=1, first_name="Дима", last_name=None, username="me")
    peer = User(id=2, first_name="Аня", last_name=None, username="anya")
    chat = User(id=2, first_name="Аня", username="anya")
    client = SimpleNamespace(get_me=AsyncMock(return_value=me))

    with patch.object(userbot, "own_message_count", AsyncMock(return_value=3)), \
         patch.object(userbot, "min_own_messages", AsyncMock(return_value=1)), \
         patch.object(userbot, "sync_message_entities", AsyncMock()) as sync:
        await userbot.save_message(client, _msg(7, chat, peer))
        await userbot.save_message(client, _msg(7, chat, peer))

    rows = await _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.source_event_id == "tg:2:7"
    assert row.account == "userbot"
    assert row.triage_status == "pending"
    assert row.metadata_["author_role"] == "counterparty"
    assert row.content_text.startswith("Author: @anya [counterparty]")
    sync.assert_awaited_once()


@pytest.mark.usefixtures("sqlite_db")
@pytest.mark.asyncio
async def test_backfill_dialog_counts_only_new_rows():
    from ingestor_telegram import backfill

    me = User(id=1, first_name="Дима", last_name=None, username="me")
    peer = User(id=2, first_name="Аня", last_name=None, username="anya")
    dialog = SimpleNamespace(id=2, title="Аня", entity=User(megagroup=False))
    msgs = [_msg(i, dialog, peer) for i in (1, 2, 3)]

    async def _iter(*_a, **_kw):
        for m in msgs:
            yield m

    client = SimpleNamespace(iter_messages=_iter)
    cutoff = datetime(2026, 10, 1)

    with patch.object(backfill, "own_message_count", AsyncMock(return_value=0)):
        first = await backfill.backfill_dialog(client, dialog, cutoff, me, 1)
        second = await backfill.backfill_dialog(client, dialog, cutoff, me, 1)

    assert (first, second) == (3, 0)
    assert await _count() == 3


@pytest.mark.usefixtures("sqlite_db")
@pytest.mark.asyncio
async def test_backfill_dialog_keeps_messages_read_before_failure():
    from ingestor_telegram import backfill

    me = User(id=1, first_name="Дима", last_name=None, username="me")
    peer = User(id=2, first_name="Аня", last_name=None, username="anya")
    dialog = SimpleNamespace(id=2, title="Аня", entity=User(megagroup=False))

    async def _iter(*_a, **_kw):
        yield _msg(1, dialog, peer)
        raise ConnectionError("flood")

    client = SimpleNamespace(iter_messages=_iter)

    with patch.object(backfill, "own_message_count", AsyncMock(return_value=0)):
        inserted = await backfill.backfill_dialog(
            client, dialog, datetime(2026, 10, 1), me, 1)

    assert inserted == 1
    assert await _count() == 1

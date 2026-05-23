"""Telegram backfill streamer.

Walks every dialog visible to the userbot, fetches messages back to
`since` (oldest first), runs each through the same envelope builder
the live poller uses, applies the source's filter rules, and yields
envelope dicts. Consumed by vera-core sources/telegram.py over NDJSON.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone

from sqlalchemy import select
from telethon.errors import FloodWaitError
from telethon.tl.types import Message

from vera_shared.db.engine import get_session
from vera_shared.db.models import Source
from vera_shared.sources import evaluate

from app.poller import _build_payload, _refresh_folders
from app.userbot.client import get_client

log = logging.getLogger(__name__)

_BATCH = 100


async def stream_envelopes(source_name: str, since: date) -> AsyncIterator[dict]:
    client = get_client()
    if not client.is_connected() or not await client.is_user_authorized():
        log.warning("backfill: userbot not connected")
        return

    async with get_session() as s:
        src = (await s.execute(
            select(Source).where(Source.type == "telegram",
                                  Source.name == source_name)
        )).scalar_one_or_none()
    if src is None:
        log.warning("backfill: source %s not found", source_name)
        return

    me = await client.get_me()
    folder_map = await _refresh_folders()
    since_dt = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    yielded = 0

    async for dialog in client.iter_dialogs():
        try:
            async for msg in client.iter_messages(
                dialog.entity, offset_date=None, reverse=False,
            ):
                if not isinstance(msg, Message):
                    continue
                if msg.date and msg.date < since_dt:
                    break  # iter is newest-first, so we're done with this dialog
                # Skip own messages
                if (getattr(msg, "from_id", None)
                        and getattr(msg.from_id, "user_id", None) == me.id):
                    continue
                try:
                    payload = await _build_payload(
                        src.name, dialog, msg, me.id, folder_map
                    )
                except Exception as exc:
                    log.debug("build_payload failed in backfill: %s", exc)
                    continue
                filter_p = payload.pop("_filter_payload")
                decision = evaluate(src.filters, filter_p)
                if decision == "exclude":
                    continue
                if decision == "priority":
                    payload["category"] = "priority"
                # Re-shape to v3 envelope: drop legacy "category" key, keep
                # everything else 1:1 with EventEnvelope fields.
                payload.pop("category", None)
                yielded += 1
                yield payload
        except FloodWaitError as exc:
            log.warning("backfill flood wait %ds on dialog %s",
                        exc.seconds, getattr(dialog.entity, "id", "?"))
        except Exception as exc:
            log.warning("backfill dialog %s failed: %s",
                        getattr(dialog.entity, "id", "?"), exc)

    log.info("backfill done: source=%s yielded=%d", source_name, yielded)

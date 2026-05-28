"""Background poller — reads Instagram DMs for connected accounts.

Every poll_interval_sec per account:
  1. Get direct inbox threads via instagrapi
  2. For each message newer than last_dm_cursor (ISO timestamp):
     a. keyword match against IgAutoReply rules → auto-reply
     b. no match → push into Vera's /event pipeline (triage as normal)
  3. Update last_polled_at + last_dm_cursor
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import IgAccount, IgAutoReply

log = logging.getLogger(__name__)

_POLL_TICK = 60   # global tick; each account uses its own poll_interval_sec


def start() -> None:
    from app.common.bg import spawn
    spawn(_poll_loop(), name="ig-poller")
    log.info("Instagram DM poller started")


async def _poll_loop() -> None:
    while True:
        try:
            await _tick()
        except Exception as exc:
            log.exception("ig_poller tick: %s", exc)
        await asyncio.sleep(_POLL_TICK)


async def _tick() -> None:
    now = datetime.utcnow()
    async with get_session() as s:
        accounts = (await s.execute(
            select(IgAccount).where(
                IgAccount.enabled == True,
                IgAccount.status == "ok",
            )
        )).scalars().all()

    for acc in accounts:
        due = (acc.last_polled_at is None or
               (now - acc.last_polled_at).total_seconds() >= acc.poll_interval_sec)
        if not due:
            continue
        try:
            await _poll_account(acc)
        except Exception as exc:
            log.warning("ig @%s poll failed: %s", acc.username, exc)
            async with get_session() as s:
                row = await s.get(IgAccount, acc.id)
                if row:
                    row.last_error = str(exc)[:500]
                    row.status = "error"
                    await s.commit()


async def _poll_account(acc: IgAccount) -> None:
    from app.instagram.client import get_client
    cl = await get_client(acc.username)
    if cl is None:
        return

    # last_dm_cursor stores ISO timestamp of last processed message
    since: datetime | None = None
    if acc.last_dm_cursor:
        try:
            since = datetime.fromisoformat(acc.last_dm_cursor)
        except ValueError:
            since = None

    threads = await asyncio.to_thread(cl.direct_threads, amount=20)
    new_count = 0
    latest_ts: datetime | None = None

    for thread in threads:
        for msg in (thread.messages or []):
            if not msg.text:
                continue
            # instagrapi timestamp is timezone-aware; normalise to naive UTC
            ts = msg.timestamp
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            if since and ts <= since:
                continue
            # Skip messages sent by Vera's own account
            sender_id = str(msg.user_id)
            if sender_id == str(acc.business_account_id):
                continue
            # Resolve sender username for logging / rule matching
            try:
                user_info = await asyncio.to_thread(cl.user_info, msg.user_id)
                sender = user_info.username
            except Exception:
                sender = str(msg.user_id)

            await _handle_dm(acc, cl, sender, str(msg.user_id), msg.text.strip(), msg)
            new_count += 1
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts

    async with get_session() as s:
        row = await s.get(IgAccount, acc.id)
        if row:
            row.last_polled_at = datetime.utcnow()
            row.last_error = None
            row.status = "ok"
            if latest_ts:
                row.last_dm_cursor = latest_ts.isoformat()
            await s.commit()

    if new_count:
        log.info("ig @%s: %d new DMs processed", acc.username, new_count)


async def _handle_dm(acc: IgAccount, cl, sender: str, sender_id: str,
                     text: str, raw) -> None:
    rule = await _match_rule(acc.username, text)
    if rule:
        await _auto_reply(acc, cl, sender_id, rule)
        return
    await _push_event(acc, sender, text, raw)


async def _match_rule(username: str, text: str) -> "IgAutoReply | None":
    text_lower = text.lower()
    async with get_session() as s:
        rules = (await s.execute(
            select(IgAutoReply).where(
                IgAutoReply.account_username == username,
                IgAutoReply.enabled == True,
            ).order_by(IgAutoReply.id)
        )).scalars().all()
    for rule in rules:
        kws = rule.trigger_keywords or []
        if kws and all(k in text_lower for k in kws):
            return rule
    return None


async def _auto_reply(acc: IgAccount, cl, sender_id: str,
                      rule: "IgAutoReply") -> None:
    try:
        await asyncio.to_thread(
            cl.direct_send, rule.response_template, user_ids=[int(sender_id)]
        )
        async with get_session() as s:
            row = await s.get(IgAutoReply, rule.id)
            if row:
                row.match_count = (row.match_count or 0) + 1
                row.last_matched_at = datetime.utcnow()
                await s.commit()
        log.info("ig @%s: auto-replied (rule %d)", acc.username, rule.id)
    except Exception as exc:
        log.warning("ig auto-reply rule %d failed: %s", rule.id, exc)


async def _push_event(acc: IgAccount, sender: str, text: str, raw) -> None:
    import httpx
    from app.config import get_settings
    settings = get_settings()
    payload = {
        "source": "instagram",
        "source_event_id": f"ig:{acc.username}:{getattr(raw, 'id', '')}",
        "account": acc.username,
        "category": "dm",
        "content_text": text,
        "entity_hints": [{"type": "person", "identifier": sender, "name": sender}],
        "metadata": {"ig_account": acc.username, "sender": sender},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "http://localhost:8000/event",
                json=payload,
                headers={"X-Internal-Secret": settings.internal_secret},
            )
            r.raise_for_status()
    except Exception as exc:
        log.warning("ig push_event @%s/%s: %s", acc.username, sender, exc)

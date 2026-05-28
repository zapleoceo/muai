"""Background poller — checks Instagram DMs for connected accounts.

Every poll_interval_sec per account:
  1. Fetch recent DM conversations via MCP instagram tool
  2. For each new message not yet seen:
     a. Check IgAutoReply rules (keyword match) → if match: auto-reply + log
     b. Otherwise: push to /event endpoint → Vera triages normally
  3. Update last_polled_at + last_dm_cursor
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import IgAccount, IgAutoReply

log = logging.getLogger(__name__)

_POLL_TICK = 60  # wake up every 60s, skip accounts not due yet


def start() -> None:
    from app.common.bg import spawn
    spawn(_poll_loop(), name="ig-poller")
    log.info("Instagram DM poller started")


async def _poll_loop() -> None:
    while True:
        try:
            await _tick()
        except Exception as exc:
            log.exception("ig_poller tick failed: %s", exc)
        await asyncio.sleep(_POLL_TICK)


async def _tick() -> None:
    now = datetime.utcnow()
    async with get_session() as s:
        accounts = (await s.execute(
            select(IgAccount).where(IgAccount.enabled == True,
                                    IgAccount.status == "ok")
        )).scalars().all()

    for acc in accounts:
        due = (acc.last_polled_at is None or
               (now - acc.last_polled_at).total_seconds() >= acc.poll_interval_sec)
        if not due:
            continue
        try:
            await _poll_account(acc)
        except Exception as exc:
            log.warning("ig poll @%s failed: %s", acc.username, exc)
            async with get_session() as s:
                row = await s.get(IgAccount, acc.id)
                if row:
                    row.last_error = str(exc)[:500]
                    row.status = "error"
                    await s.commit()


async def _poll_account(acc: IgAccount) -> None:
    """Poll one account for new DMs."""
    server_name = f"instagram-{acc.username}"
    try:
        from app.mcp.manager import call_tool as mcp_call
        result = await mcp_call(server_name, "get_dm_conversations", {"limit": 20})
    except Exception as exc:
        raise RuntimeError(f"MCP call failed: {exc}") from exc

    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown MCP error"))

    conversations = (result.get("result") or {}).get("conversations") or []
    new_count = 0
    for conv in conversations:
        msgs = conv.get("messages") or []
        for msg in msgs:
            msg_id = str(msg.get("id") or "")
            if not msg_id or msg_id == acc.last_dm_cursor:
                break
            sender = msg.get("from", {}).get("username") or "unknown"
            if sender == acc.username:
                continue  # skip own messages
            text = (msg.get("text") or msg.get("message") or "").strip()
            if not text:
                continue
            await _handle_dm(acc, sender, text, msg_id, msg)
            new_count += 1

    async with get_session() as s:
        row = await s.get(IgAccount, acc.id)
        if row:
            row.last_polled_at = datetime.utcnow()
            row.last_error = None
            row.status = "ok"
            if conversations:
                first_msg_id = (conversations[0].get("messages") or [{}])[0].get("id")
                if first_msg_id:
                    row.last_dm_cursor = str(first_msg_id)
            await s.commit()

    if new_count:
        log.info("ig @%s: %d new DMs processed", acc.username, new_count)


async def _handle_dm(acc: IgAccount, sender: str, text: str,
                     msg_id: str, raw: dict) -> None:
    """Route a DM: auto-reply if rule matches, else push to event system."""
    rule = await _match_rule(acc.username, text)
    if rule:
        await _auto_reply(acc, sender, rule, text, msg_id)
        return
    # No rule match → feed into Vera's event pipeline
    await _push_event(acc, sender, text, msg_id, raw)


async def _match_rule(username: str, text: str) -> "IgAutoReply | None":
    """Return first matching enabled rule for this account, or None."""
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


async def _auto_reply(acc: IgAccount, sender: str, rule: "IgAutoReply",
                      original_text: str, msg_id: str) -> None:
    server_name = f"instagram-{acc.username}"
    try:
        from app.mcp.manager import call_tool as mcp_call
        await mcp_call(server_name, "send_dm", {
            "recipient_username": sender,
            "message": rule.response_template,
        })
        async with get_session() as s:
            row = await s.get(IgAutoReply, rule.id)
            if row:
                row.match_count = (row.match_count or 0) + 1
                row.last_matched_at = datetime.utcnow()
                await s.commit()
        log.info("ig @%s: auto-replied to @%s (rule %d)", acc.username, sender, rule.id)
    except Exception as exc:
        log.warning("ig auto-reply failed (rule %d): %s", rule.id, exc)


async def _push_event(acc: IgAccount, sender: str, text: str,
                      msg_id: str, raw: dict) -> None:
    """Push Instagram DM into Vera's /event endpoint."""
    import httpx
    from app.config import get_settings
    settings = get_settings()
    payload = {
        "source": "instagram",
        "source_event_id": f"ig:{acc.username}:{msg_id}",
        "account": acc.username,
        "category": "dm",
        "content_text": text,
        "entity_hints": [{"type": "person", "identifier": sender, "name": sender}],
        "metadata": {"raw": raw, "ig_account": acc.username},
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
        log.warning("ig push_event failed for @%s/%s: %s", acc.username, sender, exc)

"""Guarded send-as-Dima: the ONLY path that posts a message from the userbot.

Used by ``POST /actions/send_message`` (tools_http.py) — the monthly banya
report cron (scripts/banya_monthly_report.sh) is its single caller.

Three locks, each enough on its own to stop a stray post:
  1. path is ``/actions/*``, not ``/tools/*`` — brain-search's agent can only
     reach ``/tools/{name}`` (agent.py ``_exec_http_telegram``), so the LLM
     cannot call this even by guessing a name;
  2. its own secret ``TG_SEND_SECRET`` — NOT ``INTERNAL_SECRET``, which every
     service holds; only the userbot container has this one;
  3. chat allowlist ``TG_SEND_ALLOWED_CHATS`` (comma-separated marked ids,
     e.g. ``-1003799072880``). Empty → every send is refused.
"""
from __future__ import annotations

import hmac
import os
from typing import Any

MAX_TEXT = 4000


def allowed_chats() -> set[str]:
    raw = os.environ.get("TG_SEND_ALLOWED_CHATS", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def check_send_request(
    secret_header: str | None,
    chat_id: Any,
    text: Any,
    *,
    send_secret: str,
    allowed: set[str],
) -> tuple[int, str] | None:
    """Return (http_status, reason) to refuse, or None when the send may go."""
    if not send_secret or not hmac.compare_digest(secret_header or "", send_secret):
        return 401, "X-Send-Secret required"
    cid = str(chat_id or "").strip()
    if not cid or cid not in allowed:
        return 403, "chat is not in TG_SEND_ALLOWED_CHATS"
    if not isinstance(text, str) or not text.strip():
        return 400, "text is empty"
    if len(text) > MAX_TEXT:
        return 400, f"text longer than {MAX_TEXT}"
    return None


async def send_to_chat(client: Any, chat_id: int, text: str) -> int:
    """Send ``text`` to ``chat_id``; returns the new message id.

    StringSession keeps no entity cache across restarts, so ``get_entity`` by
    a bare id can fail with "Could not find the input entity" — fall back to
    walking the dialog list, which also warms the cache.
    """
    try:
        entity = await client.get_entity(chat_id)
    except ValueError:
        entity = None
        async for d in client.iter_dialogs():
            if d.id == chat_id:
                entity = d.entity
                break
        if entity is None:
            raise LookupError(f"chat {chat_id} not found among dialogs") from None
    msg = await client.send_message(entity, text)
    return int(msg.id)

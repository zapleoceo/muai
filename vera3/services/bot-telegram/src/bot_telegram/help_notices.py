"""Idempotent help phase notices, separate from the executor's final receipt."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from html import escape

from vera_shared.voice_help.delivery_claim import claim, classify_send_error, settle

log = logging.getLogger(__name__)


async def send_help_notice(send: Callable[[str, str], Awaitable[int]], command_id: str,
                           phase: str, text: str, now: datetime) -> bool:
    """Keep live/proven-unsent attempts pending; unknown notices are never replayed."""
    key = f"help_notice_{phase}"
    held = await claim(command_id, "help_notice", now, key)
    if held is not None:
        if held.get("state") == "claimed":
            try:
                stale = now - datetime.fromisoformat(held["at"]) >= timedelta(minutes=5)
            except (ValueError, TypeError, KeyError):
                stale = True
            if not stale:
                return False  # Another live sender must settle before phase advancement.
            if not await settle(command_id, "help_notice", "unknown", key=key, expected=held):
                return False  # The sender already settled; observe its newer outcome next tick.
        return True
    try:
        receipt = await send(escape(text, quote=False), text)
    except Exception as exc:
        state = classify_send_error(exc)
        await settle(command_id, "help_notice", state, key=key)
        log.warning("help notice %s/%s: %s (%s)", command_id, phase, state, type(exc).__name__)
        return state != "error"
    state = "sent" if type(receipt) is int and receipt > 0 else "unknown"
    await settle(command_id, "help_notice", state,
                 receipt if state == "sent" else None, key=key)
    return True

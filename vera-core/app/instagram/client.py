"""Instagrapi client pool — one authenticated client per account.

Session JSON persisted encrypted in IgAccount.access_token_enc.
Sessions last months; on auth failure status → error so poller skips the account.
All instagrapi calls are blocking (sync) — wrapped in asyncio.to_thread().
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import IgAccount

log = logging.getLogger(__name__)

_pool: dict[str, object] = {}  # username → instagrapi.Client


async def get_client(username: str):
    """Return a live instagrapi Client or None if account not connected."""
    if username in _pool:
        return _pool[username]
    return await _restore(username)


async def _restore(username: str):
    from instagrapi import Client
    async with get_session() as s:
        acc = (await s.execute(
            select(IgAccount).where(IgAccount.username == username)
        )).scalar_one_or_none()
    if not acc or not acc.access_token_enc:
        return None
    try:
        session_data = json.loads(_decrypt(acc.access_token_enc))
    except Exception as e:
        log.warning("ig @%s: bad session data: %s", username, e)
        return None
    cl = Client()
    try:
        await asyncio.to_thread(cl.set_settings, session_data)
        await asyncio.to_thread(cl.get_timeline_feed)
        _pool[username] = cl
        log.info("ig @%s: session restored", username)
        return cl
    except Exception as e:
        log.warning("ig @%s: session invalid — %s", username, e)
        await _mark_error(username, str(e))
        return None


async def login(username: str, password: str) -> tuple[str, str]:
    """Fresh login. Returns (encrypted_session_json, user_id).

    Raises on 2FA challenge — caller should surface the error to the user.
    """
    from instagrapi import Client
    cl = Client()
    await asyncio.to_thread(cl.login, username, password)
    settings = await asyncio.to_thread(cl.get_settings)
    enc = _encrypt(json.dumps(settings))
    user_id = str(cl.user_id)
    _pool[username] = cl
    log.info("ig @%s: login OK user_id=%s", username, user_id)
    return enc, user_id


def evict(username: str) -> None:
    """Remove client from pool (e.g. after delete or logout)."""
    _pool.pop(username, None)


# ── crypto helpers ───────────────────────────────────────────────────────────

def _encrypt(text: str) -> str:
    from app.config import get_settings
    from vera_shared.crypto import encrypt
    return encrypt(text, get_settings().session_secret)


def _decrypt(stored: str) -> str:
    from app.config import get_settings
    from vera_shared.crypto import decrypt
    return decrypt(stored, get_settings().session_secret)


async def _mark_error(username: str, error: str) -> None:
    async with get_session() as s:
        row = (await s.execute(
            select(IgAccount).where(IgAccount.username == username)
        )).scalar_one_or_none()
        if row:
            row.status = "error"
            row.last_error = error[:500]
            row.updated_at = datetime.utcnow()
            await s.commit()

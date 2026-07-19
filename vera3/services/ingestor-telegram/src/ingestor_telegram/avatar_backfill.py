"""Throttled profile-photo backfill for graph entities.

Anti-ban first: this runs as a slow background task on the userbot session
that already handles live ingestion. It fetches at most one avatar every
AVATAR_FETCH_INTERVAL_S seconds, highest-degree entities first (the people
Dima actually sees most), backs off hard on FloodWait, and stops entirely
while the owner has the backfill paused. Entities with no photo (or that
can't be resolved from the session cache) are marked `missing` so they're
never retried in a tight loop.

Downloads go by Telegram id via the session's entity cache (the userbot has
already seen these users' messages) — no extra resolve/contact-import calls,
which are the rate-limited ones.
"""
from __future__ import annotations

import asyncio
import logging
import os

from telethon.errors import FloodWaitError
from vera_shared.control import is_backfill_paused
from vera_shared.graph.avatars import list_entities_needing_avatar, upsert_avatar

log = logging.getLogger("tg.avatars")

ENABLED = os.environ.get("AVATAR_BACKFILL_ENABLED", "true").lower() == "true"
BATCH = int(os.environ.get("AVATAR_BATCH", "20"))
FETCH_INTERVAL_S = float(os.environ.get("AVATAR_FETCH_INTERVAL_S", "4"))
# Профильные превью крошечные (download_big=False), но зависшая закачка не
# должна вешать весь backfill-цикл и держать память — жёсткий потолок.
FETCH_TIMEOUT_S = float(os.environ.get("AVATAR_FETCH_TIMEOUT_S", "30"))
IDLE_SLEEP_S = float(os.environ.get("AVATAR_IDLE_SLEEP_S", "600"))
PAUSED_SLEEP_S = float(os.environ.get("AVATAR_PAUSED_SLEEP_S", "120"))


async def _fetch_one(client, ent: dict) -> None:
    """Download+store one avatar, or mark it missing. Never raises (except to
    let the caller handle FloodWait)."""
    eid = ent["id"]
    tg_id = ent.get("tg_id")
    try:
        peer = int(tg_id) if tg_id is not None else ent.get("username")
        if peer is None:
            await upsert_avatar(eid, image=None, missing=True)
            return
        data = await asyncio.wait_for(
            client.download_profile_photo(peer, file=bytes, download_big=False),
            FETCH_TIMEOUT_S,
        )
    except FloodWaitError:
        raise
    except TimeoutError:
        log.debug("avatar fetch timed out entity=%s — marking missing", eid)
        await upsert_avatar(eid, image=None, missing=True)
        return
    except Exception as e:
        # Unresolvable from cache / privacy-hidden / deleted — mark missing so
        # we don't retry it every pass. Logged at DEBUG (expected, high volume).
        log.debug("avatar fetch failed entity=%s: %s", eid, e)
        await upsert_avatar(eid, image=None, missing=True)
        return

    if data:
        await upsert_avatar(eid, image=bytes(data), mime="image/jpeg")
    else:
        await upsert_avatar(eid, image=None, missing=True)


async def run_avatar_backfill(client) -> None:
    """Slow forever-loop. Meant to run as a background task next to live
    ingestion; degrades to sleeping when paused or nothing is pending."""
    if not ENABLED:
        log.info("avatar backfill disabled (AVATAR_BACKFILL_ENABLED=false)")
        return
    log.info("avatar backfill started (batch=%d, %.1fs/photo)", BATCH, FETCH_INTERVAL_S)
    while True:
        try:
            if await is_backfill_paused():
                await asyncio.sleep(PAUSED_SLEEP_S)
                continue
            batch = await list_entities_needing_avatar(limit=BATCH)
            if not batch:
                await asyncio.sleep(IDLE_SLEEP_S)
                continue
            for ent in batch:
                try:
                    await _fetch_one(client, ent)
                except FloodWaitError as e:
                    log.warning("FloodWait %ss during avatar backfill — backing off",
                                e.seconds)
                    await asyncio.sleep(e.seconds + 5)
                await asyncio.sleep(FETCH_INTERVAL_S)
        except Exception as e:
            log.warning("avatar backfill loop error: %s", e)
            await asyncio.sleep(IDLE_SLEEP_S)

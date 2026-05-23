"""Telegram Source — HTTP wrapper over vera-telegram's /backfill stream.

One physical Source row in `sources` table = one Telegram identity. The
backfill iterates every active row of type='telegram' and streams from
vera-telegram in turn. Filter rules and folder mapping are applied
inside vera-telegram (where the Telethon client lives).
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import date

import httpx
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Source

from app.config import get_settings
from app.sources.base import EventEnvelope, Source as SourceABC
from app.sources.gmail import _envelope_from_dict  # shared parser
from app.sources.registry import register

log = logging.getLogger(__name__)


class TelegramSource(SourceABC):
    name = "telegram"
    type = "telegram"

    async def poll(self) -> AsyncIterator[EventEnvelope]:
        if False:
            yield  # pragma: no cover
        return

    async def backfill(self, since: date) -> AsyncIterator[EventEnvelope]:
        cfg = get_settings()
        base = (getattr(cfg, "vera_telegram_url", None)
                or "http://vera-telegram:8001").rstrip("/")

        sources = await _active_telegram_sources()
        if not sources:
            log.warning("backfill: no active telegram Source rows")
            return

        async with httpx.AsyncClient(timeout=None) as c:
            for src_name in sources:
                log.info("backfill telegram: %s since=%s", src_name, since)
                async with c.stream("GET", f"{base}/backfill", params={
                    "source": src_name, "since": since.isoformat(),
                }) as r:
                    if r.status_code != 200:
                        log.warning("backfill %s returned %s",
                                    src_name, r.status_code)
                        continue
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            log.warning("backfill bad json: %s", line[:120])
                            continue
                        env = _envelope_from_dict(data)
                        if env is not None:
                            yield env


async def _active_telegram_sources() -> list[str]:
    async with get_session() as s:
        rows = (await s.execute(
            select(Source.name).where(Source.type == "telegram",
                                       Source.enabled == True)
        )).all()
    return [r[0] for r in rows]


register(TelegramSource())

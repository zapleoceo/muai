"""Gmail Source — thin HTTP wrapper over vera-gmail's /backfill stream.

The Gmail API client (OAuth, token refresh, OCR) lives in vera-gmail.
This module makes vera-gmail behave like any other Source: vera-core's
brain consumes EventEnvelope; it doesn't care that the bytes came
from another container.

One Source instance covers all GmailAccount rows — `backfill(since)`
fans out per-account.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import date, datetime

import httpx
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import GmailAccount

from app.config import get_settings
from app.sources.base import EntityHint, EventEnvelope, Source
from app.sources.registry import register

log = logging.getLogger(__name__)


class GmailSource(Source):
    name = "gmail"
    type = "gmail"

    async def poll(self) -> AsyncIterator[EventEnvelope]:
        # vera-gmail still runs its own poller loop for the live stream;
        # the v3 brain consumes those via the existing /event endpoint.
        # poll() here is a no-op iterator until we migrate the live loop.
        if False:
            yield  # pragma: no cover
        return

    async def backfill(self, since: date) -> AsyncIterator[EventEnvelope]:
        cfg = get_settings()
        base = (getattr(cfg, "vera_gmail_url", None)
                or "http://vera-gmail:8000").rstrip("/")

        accounts = await _active_accounts()
        if not accounts:
            log.warning("backfill: no active GmailAccount rows")
            return

        async with httpx.AsyncClient(timeout=None) as c:
            for email in accounts:
                log.info("backfill gmail: %s since=%s", email, since)
                url = f"{base}/backfill"
                async with c.stream("GET", url, params={
                    "email": email, "since": since.isoformat(),
                }) as r:
                    if r.status_code != 200:
                        log.warning("backfill %s returned %s", email, r.status_code)
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


async def _active_accounts() -> list[str]:
    async with get_session() as s:
        rows = (await s.execute(
            select(GmailAccount.email).where(GmailAccount.is_active == True)
        )).all()
    return [r[0] for r in rows]


def _envelope_from_dict(d: dict) -> EventEnvelope | None:
    try:
        occurred = _parse_dt(d.get("occurred_at"))
        hints = [EntityHint(**h) for h in (d.get("entity_hints") or [])
                 if isinstance(h, dict) and h.get("type") and h.get("identifier")]
        return EventEnvelope(
            source=d["source"], source_event_id=d["source_event_id"],
            occurred_at=occurred, content_text=d.get("content_text") or "",
            account=d.get("account"), entity_hints=hints,
            metadata=d.get("metadata") or {},
        )
    except Exception as exc:
        log.warning("envelope parse failed: %s", exc)
        return None


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            # Email dates are messy — fall back to now if unparseable.
            from email.utils import parsedate_to_datetime
            try:
                return parsedate_to_datetime(value)
            except (TypeError, ValueError):
                pass
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.utcnow()
    return datetime.utcnow()


# Auto-register at import (sources.registry imports this module).
register(GmailSource())

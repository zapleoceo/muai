"""Backfill streamer: page through Gmail threads from `since` to now,
yielding one envelope dict per thread. Consumed by vera-core's
sources/gmail.py via NDJSON over HTTP."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import date, datetime

import httpx

from app.credentials import get_access_token
from app.api import read_thread, _BASE, _client

log = logging.getLogger(__name__)

_PAGE_SIZE = 100


async def stream_envelopes(email: str, since: date) -> AsyncIterator[dict]:
    """Yield one envelope dict per Gmail thread >= since (oldest first
    is not guaranteed — Gmail returns newest first; we page until empty)."""
    q = (
        f"after:{since.strftime('%Y/%m/%d')} in:inbox "
        "-category:promotions -category:social -category:updates"
    )
    page_token: str | None = None
    seen = 0
    while True:
        token = await get_access_token(email)
        params: dict = {"q": q, "maxResults": _PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        async with _client(token) as c:
            r = await c.get(f"{_BASE}/threads", params=params)
        if r.status_code != 200:
            log.warning("backfill list error %s: %s", r.status_code, r.text[:200])
            return
        data = r.json()
        threads = data.get("threads") or []
        if not threads:
            return
        for t in threads:
            tid = t.get("id")
            if not tid:
                continue
            try:
                env = await _envelope_for_thread(email, tid)
                if env is not None:
                    seen += 1
                    yield env
            except Exception as exc:
                log.warning("backfill thread %s failed: %s", tid, exc)
        page_token = data.get("nextPageToken")
        if not page_token:
            log.info("backfill done for %s: %d envelopes", email, seen)
            return


async def _envelope_for_thread(email: str, thread_id: str) -> dict | None:
    full = await read_thread(email, thread_id, ocr_images=False)
    if "error" in full:
        return None
    msgs = full.get("messages") or []
    if not msgs:
        return None
    last = msgs[-1]
    subject = last.get("subject") or "(без темы)"
    sender = last.get("from") or "?"
    body = (last.get("text") or last.get("snippet") or "")[:1500]
    text = f"From: {sender}\nSubject: {subject}\nDate: {last.get('date','')}\n---\n{body}"

    occurred_iso = last.get("date_iso") or last.get("date") or datetime.utcnow().isoformat()
    return {
        "source": "gmail",
        "source_event_id": f"{email}:{thread_id}",
        "account": email,
        "occurred_at": occurred_iso,
        "content_text": text,
        "entity_hints": [
            {"type": "person", "identifier": sender, "name": sender},
            {"type": "account", "identifier": email, "name": email},
            {"type": "topic", "identifier": f"thread:{thread_id}", "name": subject},
        ],
        "metadata": {
            "subject": subject,
            "thread_id": thread_id,
            "messages_count": full.get("messages_count"),
        },
    }

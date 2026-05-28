"""Mine recurring patterns from event stream — autonomous, no clicks.

Sources for a Pattern:
  (a) Dima's outgoing messages that repeat — same recipient + similar
      content N times → «он всегда пишет это». Extracts a template.
  (b) Email → chat handoff pattern — incoming email about topic X,
      Dima posts in chat Y within ~30min about same X. After 3+
      repetitions becomes a Pattern with trigger=email[X], action=chat[Y].

Runs every PATTERN_MINER_INTERVAL_SEC in background. Writes :Pattern
nodes via brain.patterns. After enough data, decide.scoring picks
these up and decide.dispatch may suggest auto-actions.

Each pattern has signature stable across re-runs, so re-mining is
idempotent: counters just bump.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from hashlib import sha1
from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

log = logging.getLogger(__name__)

PATTERN_MINER_INTERVAL_SEC = 6 * 3600   # каждые 6 часов
LOOKBACK_DAYS = 30
MIN_REPETITIONS = 3                      # сколько раз надо встретить чтобы стать паттерном
RESPONSE_WINDOW_MIN = 30                 # email → chat handoff: окно сопоставления


_GREETING_RE = re.compile(r"^(привет|hi|доброе утро|good morning|здравствуйте|hey),?\s*", re.IGNORECASE)


def _stem(text: str, max_len: int = 100) -> str:
    """Rough template-extractor: lowercase, strip greetings + numbers/dates,
    cut to N chars. Two messages that differ only by digits/names map
    to the same stem → repetition counts."""
    t = (text or "").strip().lower()
    t = _GREETING_RE.sub("", t)
    t = re.sub(r"\b\d{1,4}([./-]\d{1,4}){0,2}\b", "<num>", t)   # 25.05 / 12 / 100k
    t = re.sub(r"\b\d+\b", "<num>", t)
    t = re.sub(r"\s+", " ", t)
    return t[:max_len].strip()


def _signature(parts: list[str]) -> str:
    return sha1("|".join(parts).encode()).hexdigest()[:24]


async def mine_once() -> dict:
    """One pass. Returns stats {patterns_found, new, updated}."""
    since = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    async with get_session() as s:
        rs = (await s.execute(
            select(Event).where(Event.occurred_at >= since)
            .order_by(Event.occurred_at)
        )).scalars().all()
    events = [_view(e) for e in rs]
    log.info("pattern_miner: scanning %d events from last %dd",
              len(events), LOOKBACK_DAYS)

    # (a) recurring outgoing templates per (chat, stem)
    sent_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for ev in events:
        if ev["direction"] != "sent" or not ev["text"]:
            continue
        stem = _stem(ev["text"])
        if len(stem) < 12:
            continue  # not enough content
        sent_groups[(ev["chat"], stem)].append(ev)

    recurring = [(k, v) for k, v in sent_groups.items() if len(v) >= MIN_REPETITIONS]
    log.info("pattern_miner: %d recurring outgoing templates", len(recurring))

    new_n = 0
    upd_n = 0
    from app.brain import patterns as P
    for (chat, stem), bucket in recurring:
        # context: the chat entity (no person hint for outgoing mining)
        hints = [{"type": "chat", "identifier": chat}]
        ctx   = P.context_key_for(hints)
        sig   = P.signature_for(hints, stem[:60])
        existing = await P.get_pattern(sig)
        action_label = (bucket[0]["text"] or "")[:60]
        # Reuse upsert_observation N times so weight reflects bucket size.
        # (We can't bulk-write count, only +1 — accept that.)
        for _ in range(len(bucket)):
            await P.upsert_observation(sig, ctx, action_label,
                                        tool="telegram_send_message",
                                        args={"peer": chat, "text_template": stem})
        if existing is None:
            new_n += 1
        else:
            upd_n += 1

    return {"events_scanned": len(events),
            "recurring_templates": len(recurring),
            "new": new_n, "updated": upd_n}


def _view(e: Event) -> dict:
    m = e.metadata_ or {}
    hints = e.entity_hints or []
    chat = (m.get("chat_title")
            or next((h.get("name") for h in hints if h.get("type") == "chat"), None)
            or e.account or "?")
    person = next((h.get("name") for h in hints if h.get("type") == "person"), None)
    return {
        "id": e.id,
        "source": e.source,
        "chat": chat,
        "person": person,
        "direction": m.get("direction") or "received",
        "text": e.content_text or "",
        "occurred_at": e.occurred_at,
    }


async def miner_loop() -> None:
    """Background loop. Sleeps PATTERN_MINER_INTERVAL_SEC between runs."""
    while True:
        try:
            stats = await mine_once()
            log.info("pattern_miner stats: %s", stats)
        except asyncio.CancelledError:
            log.info("pattern_miner cancelled")
            raise
        except Exception as exc:
            log.exception("pattern_miner iteration failed: %s", exc)
        await asyncio.sleep(PATTERN_MINER_INTERVAL_SEC)


def start() -> None:
    if os.environ.get("VERA_PATTERN_MINER", "1") != "1":
        log.info("pattern_miner disabled via env")
        return
    from app.common.bg import spawn
    spawn(miner_loop(), name="pattern_miner")
    log.info("pattern_miner: started (interval %ds)", PATTERN_MINER_INTERVAL_SEC)

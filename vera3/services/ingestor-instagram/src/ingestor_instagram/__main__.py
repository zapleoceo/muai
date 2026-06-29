"""Instagram DM polling ingestor.

Каждые POLL_INTERVAL_S секунд:
  1. Загружает session JSON из БД, восстанавливает instagrapi Client
  2. Тащит direct_threads, для каждого треда — direct_messages
  3. Дедуп через source_event_id="ig:{thread_id}:{message_id}"
  4. Сохраняет новые как event source='instagram'
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

import httpx
from sqlalchemy import select
from instagrapi import Client

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import InstagramSessionRow
from vera_shared.crypto import decrypt

log = logging.getLogger("ig")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
POLL_INTERVAL_S = int(os.environ.get("IG_POLL_INTERVAL_S", "90"))
THREADS_PER_POLL = int(os.environ.get("IG_THREADS_PER_POLL", "20"))
MSGS_PER_THREAD = int(os.environ.get("IG_MSGS_PER_THREAD", "20"))


async def load_client() -> tuple[Client, str]:
    async with get_session() as s:
        row = (await s.execute(
            select(InstagramSessionRow)
            .where(InstagramSessionRow.is_active.is_(True))
            .order_by(InstagramSessionRow.id.desc())
        )).scalar_one_or_none()
    if row is None:
        raise RuntimeError("No active Instagram session in DB")
    settings = json.loads(decrypt(row.session_json_enc))
    cl = Client()
    cl.delay_range = [2, 5]
    cl.set_settings(settings)
    return cl, row.username


async def post_event(payload: dict) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        await c.post(f"{GATEWAY_URL}/event/instagram", json=payload,
                     headers={"X-Internal-Secret": INTERNAL_SECRET})


async def _already_seen(source_event_id: str) -> bool:
    async with get_session() as s:
        existing = (await s.execute(
            select(EventRow.id).where(
                EventRow.source == "instagram",
                EventRow.source_event_id == source_event_id,
            )
        )).scalar_one_or_none()
    return existing is not None


async def poll_once(cl: Client, username: str) -> int:
    """Один цикл polling. Возвращает кол-во новых сообщений."""
    threads = await asyncio.to_thread(cl.direct_threads, amount=THREADS_PER_POLL)
    saved = 0
    for t in threads:
        try:
            msgs = await asyncio.to_thread(cl.direct_messages, t.id, amount=MSGS_PER_THREAD)
        except Exception as e:
            log.warning("thread %s msgs fetch failed: %s", t.id, e)
            continue

        chat_title = ", ".join(u.username for u in t.users) or t.thread_title or "(no users)"
        is_group = len(t.users) > 1

        for m in msgs:
            sid = f"ig:{t.id}:{m.id}"
            if await _already_seen(sid):
                continue

            sender_id = getattr(m, "user_id", None)
            direction = "sent" if sender_id == cl.user_id else "received"
            sender_username = next((u.username for u in t.users if u.pk == sender_id), None) or username

            text = (m.text or "").strip()
            if not text:
                if getattr(m, "media_share", None):
                    text = "[shared post]"
                elif getattr(m, "media", None):
                    text = "[media]"
                elif getattr(m, "voice_media", None):
                    text = "[voice]"
                elif getattr(m, "clip", None):
                    text = "[reel]"
                else:
                    text = "[non-text message]"

            author_role = "self" if direction == "sent" else "counterparty"
            author_label = "Я" if author_role == "self" else f"@{sender_username}"

            content = (
                f"Author: {author_label} [{author_role}]\n"
                f"From: @{sender_username}\n"
                f"Chat: {chat_title}\n"
                f"Date: {m.timestamp.isoformat() if m.timestamp else ''}\n"
                f"Direction: {direction}\n"
                f"---\n{text[:6000]}"
            )

            payload = {
                "source": "instagram",
                "source_event_id": sid,
                "account": username,
                "category": "group" if is_group else "user",
                "content_text": content,
                "occurred_at": (m.timestamp or datetime.utcnow()).isoformat(),
                "metadata": {
                    "thread_id": str(t.id),
                    "thread_title": chat_title,
                    "is_group": is_group,
                    "sender_id": sender_id,
                    "sender_username": sender_username,
                    "direction": direction,
                    "author_role": author_role,
                    "author_label": author_label,
                    "message_id": m.id,
                    "item_type": getattr(m, "item_type", None),
                },
            }
            try:
                await post_event(payload)
                saved += 1
            except Exception as e:
                log.warning("post_event failed for %s: %s", sid, e)
    return saved


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()

    cl, username = await load_client()
    log.info("Loaded Instagram session for @%s, poll every %ss", username, POLL_INTERVAL_S)

    while True:
        try:
            n = await poll_once(cl, username)
            if n:
                log.info("polled: %d new messages saved", n)
            # update last_polled_at
            async with get_session() as s:
                row = (await s.execute(
                    select(InstagramSessionRow).where(InstagramSessionRow.username == username)
                )).scalar_one()
                row.last_polled_at = datetime.utcnow()
        except Exception as e:
            log.exception("poll failed: %s", e)

        await asyncio.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())

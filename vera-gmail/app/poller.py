"""Periodic polling of new messages → POST /event into vera-core."""
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import GmailAccount

from app.api import list_threads, read_thread
from app.config import get_settings

log = logging.getLogger(__name__)


async def _post_event(payload: dict) -> bool:
    """Returns True if vera-core accepted the event. Gmail re-fetches on
    next poll anyway, but caller can log a per-event failure for visibility.
    """
    cfg = get_settings()
    headers = {"X-Internal-Secret": cfg.internal_secret}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{cfg.vera_core_url}/event",
                             json=payload, headers=headers)
    except Exception as exc:
        log.warning("POST /event raised: %s", exc)
        return False
    if r.status_code != 200:
        log.warning("POST /event failed (%d): %s", r.status_code, r.text[:200])
        return False
    return True


_NOISE_SENDER_HINTS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notifications@", "notification@", "alerts@", "marketing@", "newsletter",
    "promo@", "promotions@", "team@", "info@", "support@",
    "@e.", "@em.", "@email.", "@mail.",
)


def _is_noise(sender: str) -> bool:
    s = (sender or "").lower()
    return any(hint in s for hint in _NOISE_SENDER_HINTS)


async def _process_account(email: str, include_automated: bool) -> None:
    cfg = get_settings()
    minutes = cfg.poll_lookback_minutes
    # Promotions/social/updates are bulk marketing — always skip; include_automated
    # only affects per-sender noreply filter (so business automation like payment
    # receipts can still reach Vera).
    # Capture BOTH incoming (in:inbox) and outgoing (in:sent) so the
    # brain knows what Дима writes — sent IS the strongest learning
    # signal. is:unread is dropped (used to gate noise, but sent never
    # has unread state and we don't want to miss it).
    q = (
        f"newer_than:{max(minutes // 60, 1)}h (in:inbox OR in:sent) "
        "-category:promotions -category:social -category:updates"
    )
    threads = await list_threads(email, query=q, max_results=10)
    if threads and isinstance(threads, list) and threads and "error" in threads[0]:
        log.warning("list_threads error for %s: %s", email, threads[0])
        return

    for t in threads or []:
        thread_id = t.get("id")
        if not thread_id:
            continue
        # idempotency: source_event_id = thread_id, so re-polling same thread does not duplicate
        full = await read_thread(email, thread_id)
        if "error" in full:
            log.warning("read_thread error %s: %s", thread_id, full["error"])
            continue

        last = (full.get("messages") or [{}])[-1]
        subject = last.get("subject") or "(без темы)"
        sender = last.get("from") or "?"
        recipient = last.get("to") or ""
        direction = "sent" if email.lower() in sender.lower() else "received"

        # noise filter applies only to incoming — never skip own sent.
        if direction == "received" and not include_automated and _is_noise(sender):
            log.info("Skip noise: %s (include_automated=False)", sender)
            continue

        body_excerpt = (last.get("text") or last.get("snippet") or "")[:1500]
        text = (
            f"From: {sender}\n"
            f"To: {recipient}\n"
            f"Subject: {subject}\n"
            f"Date: {last.get('date','')}\n"
            f"Direction: {direction}\n"
            f"---\n{body_excerpt}"
        )
        person_hint = (recipient if direction == "sent" else sender) or sender
        entity_hints = [
            {"type": "person", "identifier": person_hint, "via": "gmail"},
            {"type": "account", "identifier": email, "platform": "gmail"},
            {"type": "thread", "identifier": thread_id, "platform": "gmail"},
        ]
        await _post_event({
            "source": "gmail",
            "source_event_id": f"{email}:{thread_id}:{last.get('id','')}",
            "account": email,
            "category": "communication",
            "content_text": text,
            "entity_hints": entity_hints,
            "metadata": {
                "subject": subject,
                "thread_id": thread_id,
                "messages_count": full.get("messages_count"),
                "direction": direction,
            },
        })

    # update poll state
    async with get_session() as session:
        result = await session.execute(
            select(GmailAccount).where(GmailAccount.email == email)
        )
        row = result.scalar_one_or_none()
        if row:
            row.last_polled_at = datetime.utcnow()
            await session.commit()


async def poll_loop() -> None:
    cfg = get_settings()
    log.info("Gmail poller started (interval=%ds)", cfg.poll_interval_sec)
    while True:
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(GmailAccount.email, GmailAccount.include_automated)
                    .where(GmailAccount.is_active == True)
                )
                accounts = [(r[0], bool(r[1])) for r in result.all()]
            for email, include_automated in accounts:
                try:
                    await _process_account(email, include_automated)
                except Exception as exc:
                    log.warning("poll account %s error: %s", email, exc)
        except Exception as exc:
            log.exception("poller iteration crashed: %s", exc)
        await asyncio.sleep(cfg.poll_interval_sec)

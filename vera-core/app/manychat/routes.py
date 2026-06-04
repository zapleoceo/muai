"""ManyChat inbound webhook.

ManyChat's «External Request» action in Automation can POST to us
whenever an IG event fires (new DM, comment, follow, story reply).

Free plan = inbound only — Send API is locked. That matches our current
"learning mode": Vera observes, doesn't yet reply.

Security: shared secret in the `X-Manychat-Secret` header, matched
against env `MANYCHAT_WEBHOOK_SECRET`. Configure the same value in
ManyChat → External Request → Headers.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from app.events.ingest import schedule_ingest
from app.events.store import save_event

log = logging.getLogger(__name__)
router = APIRouter()


def _check_secret(provided: str | None) -> None:
    expected = os.environ.get("MANYCHAT_WEBHOOK_SECRET", "").strip()
    if not expected:
        # Misconfigured — refuse to accept anything until secret is set.
        raise HTTPException(503, "MANYCHAT_WEBHOOK_SECRET not configured")
    if not provided or provided.strip() != expected:
        raise HTTPException(401, "bad manychat secret")


def _build_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a free-form ManyChat External Request body to our event shape.

    ManyChat lets you craft arbitrary JSON. We're forgiving — anything we
    don't recognize is preserved under `content_extra`.
    """
    trigger = str(payload.get("trigger") or payload.get("event") or "manychat")
    text = (
        payload.get("text")
        or payload.get("last_input_text")
        or payload.get("message")
        or ""
    )
    contact_id = str(
        payload.get("contact_id")
        or payload.get("subscriber_id")
        or payload.get("user_id")
        or ""
    )
    contact_name = (
        payload.get("contact_name")
        or payload.get("full_name")
        or payload.get("name")
    )
    ig_username = payload.get("ig_username") or payload.get("instagram_username")
    page_id = str(payload.get("page_id") or payload.get("account_id") or "")
    page_name = payload.get("page_name") or payload.get("account_name")

    entity_hints: list[dict[str, Any]] = []
    if contact_id:
        entity_hints.append({
            "type": "person",
            "identifier": f"manychat:{contact_id}",
            "name": contact_name or ig_username or contact_id,
            "extra": {"ig_username": ig_username} if ig_username else None,
        })
    if page_id:
        entity_hints.append({
            "type": "account",
            "identifier": f"manychat:page:{page_id}",
            "name": page_name or page_id,
        })

    # Make source_event_id stable per ManyChat trigger so retries dedup.
    # ManyChat doesn't expose a message_id reliably on Free; we fall back
    # to (contact_id + ts) which is unique enough for inbound DMs.
    ts = payload.get("timestamp") or datetime.utcnow().isoformat()
    source_event_id = (
        payload.get("message_id")
        or f"{contact_id}:{ts}"
        or None
    )

    return {
        "source": "instagram",
        "source_event_id": source_event_id,
        "account": page_id or None,
        "category": trigger,  # "new_message" | "new_comment" | ...
        "content_text": text,
        "content_extra": payload,
        "entity_hints": entity_hints,
        "metadata": {"channel": "manychat", "page_name": page_name},
        "occurred_at": _parse_ts(ts),
    }


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.utcnow()


@router.post("/event/manychat")
async def manychat_webhook(
    request: Request,
    x_manychat_secret: str | None = Header(default=None),
) -> dict:
    _check_secret(x_manychat_secret)
    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("manychat webhook: bad JSON: %s", exc)
        raise HTTPException(400, "invalid JSON")

    if not isinstance(payload, dict):
        raise HTTPException(400, "payload must be JSON object")

    spec = _build_event(payload)
    event, is_new = await save_event(**spec)
    if not is_new:
        return {"ok": True, "event_id": event.id, "deduped": True}

    schedule_ingest(
        event.id,
        source=event.source,
        category=event.category,
        content_text=event.content_text,
        entity_hints=event.entity_hints,
        metadata=event.metadata_,
        occurred_at=event.occurred_at,
    )
    log.info("manychat → event %s (%s)", event.id, spec["category"])
    return {"ok": True, "event_id": event.id}


@router.get("/api/manychat/status")
async def manychat_status() -> dict:
    """Lightweight health probe for the dashboard."""
    return {
        "enabled": bool(os.environ.get("MANYCHAT_WEBHOOK_SECRET", "").strip()),
        "secret_set": bool(os.environ.get("MANYCHAT_WEBHOOK_SECRET", "").strip()),
        "outbound_send_api": False,  # Free plan
        "webhook_url": "/event/manychat",
    }

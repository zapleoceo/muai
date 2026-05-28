import logging

from app.userbot.client import get_client

log = logging.getLogger(__name__)


async def send_message_to(peer: str, text: str) -> dict:
    """Send a message AND immediately ingest it to brain as direction=sent.

    Why: Telethon's NewMessage(outgoing=True) doesn't fire for messages
    sent by the SAME client (known quirk). So if the bot/Vera sends on
    behalf of Дима, the brain would never see those sends. We manually
    post the event here so sent ALWAYS lands in graph.
    """
    client = get_client()
    resolved: str | int = peer
    if peer.lstrip("-").isdigit():
        resolved = int(peer)
    msg = await client.send_message(resolved, text)
    # Push to brain as 'sent' (best-effort, never break send).
    try:
        from app.poller import _build_payload, _post_event, _refresh_folders
        from sqlalchemy import select
        from vera_shared.db.engine import get_session
        from vera_shared.db.models import Source
        me = await client.get_me()
        async with get_session() as s:
            src = (await s.execute(
                select(Source).where(Source.type == "telegram",
                                       Source.enabled == True).limit(1)
            )).scalar_one_or_none()
        if src is not None:
            entity = await client.get_entity(resolved)

            class _Shim:
                def __init__(self, entity):
                    self.entity = entity
            folder_map = await _refresh_folders()
            payload = await _build_payload(src.name, _Shim(entity), msg, me.id, folder_map)
            payload.setdefault("metadata", {})["direction"] = "sent"
            payload.pop("_filter_payload", None)
            await _post_event(payload)
    except Exception as exc:
        log.warning("send_message_to brain ingest failed: %s", exc)
    return {"message_id": msg.id, "date": msg.date.isoformat() if msg.date else None}

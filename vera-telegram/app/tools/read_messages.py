from datetime import datetime, timedelta, timezone

from app.userbot.client import get_client


def _sender_name(msg) -> str:
    sender = getattr(msg, "_sender", None)
    if sender is None:
        return "unknown"
    fn = getattr(sender, "first_name", None) or ""
    ln = getattr(sender, "last_name", None) or ""
    title = getattr(sender, "title", None)
    return title or " ".join(filter(None, [fn, ln])) or str(sender.id)


async def read_messages(
    peer: str, limit: int = 20, offset_days: int = 1
) -> list[dict]:
    client = get_client()
    cutoff = datetime.now(timezone.utc) - timedelta(days=offset_days)

    # Try numeric ID first
    resolved_peer: str | int = peer
    if peer.lstrip("-").isdigit():
        resolved_peer = int(peer)

    messages: list[dict] = []
    async for msg in client.iter_messages(resolved_peer, limit=limit):
        if msg.date and msg.date < cutoff:
            break
        await msg._get_sender()
        messages.append({
            "id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
            "text": msg.text or "",
            "sender_name": _sender_name(msg),
            "out": msg.out,
        })
    return messages

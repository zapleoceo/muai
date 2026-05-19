from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError

from app.userbot.client import get_client


def _sender_name(msg) -> str:
    sender = getattr(msg, "_sender", None) or getattr(msg, "sender", None)
    if sender is None:
        return "unknown"
    title = getattr(sender, "title", None)
    if title:
        return title
    fn = getattr(sender, "first_name", None) or ""
    ln = getattr(sender, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(getattr(sender, "id", ""))


def _entity_name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    fn = getattr(entity, "first_name", None) or ""
    ln = getattr(entity, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(getattr(entity, "id", ""))


async def _resolve_peer_by_id_or_name(peer: str):
    client = get_client()
    if peer.lstrip("-").isdigit():
        return await client.get_entity(int(peer))
    # Search ONLY user's own dialogs (no global Telegram search)
    q = peer.lower()
    try:
        async for d in client.iter_dialogs(limit=500):
            name = _entity_name(d.entity).lower()
            if q in name:
                return d.entity
    except FloodWaitError as exc:
        raise LookupError(f"telegram flood wait {exc.seconds}s") from exc
    raise LookupError(
        f"chat '{peer}' not found in your dialogs — use telegram_search_dialogs to find the chat_id"
    )


async def read_messages(peer: str, limit: int = 50, offset_days: int = 1) -> dict:
    if not peer:
        raise LookupError("peer empty — call telegram_search_dialogs first")
    client = get_client()
    entity = await _resolve_peer_by_id_or_name(peer)
    cutoff = datetime.now(timezone.utc) - timedelta(days=offset_days)

    messages: list[dict] = []
    async for msg in client.iter_messages(entity, limit=limit):
        if msg.date and msg.date < cutoff:
            break
        await msg.get_sender()
        messages.append({
            "id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
            "text": msg.text or "",
            "from": _sender_name(msg),
            "out": msg.out,
        })

    return {
        "chat_id": entity.id,
        "chat_name": _entity_name(entity),
        "messages_count": len(messages),
        "messages": messages,
    }

from datetime import datetime, timedelta, timezone

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
    return " ".join(filter(None, [fn, ln])) or str(entity.id)


async def _resolve_peer(peer: str):
    client = get_client()
    if not peer:
        raise LookupError("peer is empty — укажи с кем переписку читать")

    if peer.lstrip("-").isdigit():
        return await client.get_entity(int(peer))

    # Try exact resolution first (works for usernames and full names in contacts)
    try:
        return await client.get_entity(peer)
    except Exception:
        pass

    # Fallback: fuzzy match against dialog list
    q = peer.lower()
    best = None
    async for dialog in client.iter_dialogs():
        name = _entity_name(dialog.entity).lower()
        if q in name:
            if best is None or len(name) < len(_entity_name(best).lower()):
                best = dialog.entity
    if best is None:
        raise LookupError(f"диалог с «{peer}» не найден")
    return best


async def read_messages(
    peer: str, limit: int = 30, offset_days: int = 1
) -> list[dict]:
    client = get_client()
    entity = await _resolve_peer(peer)
    chat_name = _entity_name(entity)
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
            "_chat_name": chat_name,
        })
    return messages

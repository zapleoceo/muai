from datetime import datetime, timedelta, timezone

from telethon.tl.functions.contacts import SearchRequest

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


async def _resolve_peer(peer: str):
    client = get_client()
    if not peer:
        raise LookupError("peer is empty — укажи с кем переписку читать")

    if peer.lstrip("-").isdigit():
        return await client.get_entity(int(peer))

    try:
        return await client.get_entity(peer)
    except Exception:
        pass

    # Server-side search — does NOT iterate all dialogs, no flood wait
    res = await client(SearchRequest(q=peer, limit=10))
    candidates = list(res.users) + list(res.chats)
    if not candidates:
        raise LookupError(f"диалог с «{peer}» не найден (попробуй точное имя или @username)")

    q = peer.lower()
    exact = [e for e in candidates if q == _entity_name(e).lower()]
    if exact:
        return exact[0]
    starts = [e for e in candidates if _entity_name(e).lower().startswith(q)]
    return (starts or candidates)[0]


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

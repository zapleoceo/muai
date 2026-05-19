from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError
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


async def _candidates_from_recent(query: str, limit: int = 200) -> list[tuple]:
    """Return [(dialog, entity, last_date), ...] matching query, sorted by recency."""
    client = get_client()
    q = query.lower()
    matches: list[tuple] = []
    try:
        async for d in client.iter_dialogs(limit=limit):
            name = _entity_name(d.entity).lower()
            if q in name:
                matches.append((d, d.entity, d.date))
    except FloodWaitError:
        return []
    matches.sort(key=lambda x: x[2] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return matches


async def _resolve_peer(peer: str) -> tuple:
    """Returns (entity, resolution_note). Raises LookupError if nothing found."""
    client = get_client()
    if not peer:
        raise LookupError("peer пустой — укажи с кем читать переписку")

    if peer.lstrip("-").isdigit():
        e = await client.get_entity(int(peer))
        return e, ""

    try:
        e = await client.get_entity(peer)
        return e, ""
    except Exception:
        pass

    recent = await _candidates_from_recent(peer, limit=200)
    if recent:
        _, entity, _ = recent[0]
        if len(recent) > 1:
            others = ", ".join(_entity_name(e) for _, e, _ in recent[1:4])
            note = f"найдено {len(recent)} совпадений, выбрал самый активный «{_entity_name(entity)}» (другие: {others})"
        else:
            note = ""
        return entity, note

    res = await client(SearchRequest(q=peer, limit=10))
    entities = list(res.users) + list(res.chats)
    if not entities:
        raise LookupError(f"диалог с «{peer}» не найден")
    return entities[0], f"в недавних не нашёл, взял через серверный поиск: «{_entity_name(entities[0])}»"


async def read_messages(
    peer: str, limit: int = 50, offset_days: int = 1
) -> dict:
    client = get_client()
    entity, note = await _resolve_peer(peer)
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
        })

    return {
        "chat_name": chat_name,
        "chat_id": entity.id,
        "resolution_note": note,
        "messages_count": len(messages),
        "messages": messages,
    }

from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User

from app.userbot.client import get_client


def _type(entity) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    return "unknown"


def _name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    fn = getattr(entity, "first_name", None) or ""
    ln = getattr(entity, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(entity.id)


async def list_recent_dialogs(
    limit: int = 20, exclude_channels: bool = False, only_unread: bool = False
) -> list[dict]:
    """Return user's most recently active dialogs, sorted by last message date desc."""
    client = get_client()
    out: list[dict] = []
    try:
        async for d in client.iter_dialogs(limit=max(limit * 3, 100)):
            t = _type(d.entity)
            if exclude_channels and t == "channel":
                continue
            if only_unread and not d.unread_count:
                continue
            out.append({
                "id": d.entity.id,
                "name": _name(d.entity),
                "type": t,
                "username": getattr(d.entity, "username", None),
                "unread_count": d.unread_count,
                "last_message_date": d.date.isoformat() if d.date else None,
            })
            if len(out) >= limit:
                break
    except FloodWaitError as exc:
        return [{"_error": f"flood wait {exc.seconds}s", "_partial": out}]
    return out

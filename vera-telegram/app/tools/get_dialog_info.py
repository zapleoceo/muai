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


async def get_dialog_info_for(peer: str) -> dict:
    client = get_client()
    resolved: str | int = peer
    if peer.lstrip("-").isdigit():
        resolved = int(peer)
    e = await client.get_entity(resolved)
    return {
        "id": e.id,
        "name": _name(e),
        "type": _type(e),
        "username": getattr(e, "username", None),
        "participants_count": getattr(e, "participants_count", None),
    }

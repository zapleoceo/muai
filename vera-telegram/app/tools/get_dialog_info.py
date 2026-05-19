from telethon.tl.types import User, Chat, Channel

from app.userbot.client import get_client


def _entity_type(entity) -> str:
    if isinstance(entity, User):
        return "user"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    return "unknown"


def _entity_name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    fn = getattr(entity, "first_name", None) or ""
    ln = getattr(entity, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(entity.id)


async def get_dialog_info(peer: str) -> dict:
    client = get_client()

    resolved_peer: str | int = peer
    if peer.lstrip("-").isdigit():
        resolved_peer = int(peer)

    entity = await client.get_entity(resolved_peer)
    participants_count = getattr(entity, "participants_count", None)

    return {
        "id": entity.id,
        "name": _entity_name(entity),
        "type": _entity_type(entity),
        "participants_count": participants_count,
    }

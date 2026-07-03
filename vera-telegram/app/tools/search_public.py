from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import SearchRequest

from app.tools.search_dialogs import _name, _type
from app.userbot.client import get_client


async def search_public(query: str, limit: int = 20) -> list[dict]:
    """Global Telegram search for public groups/channels/users — including
    ones the user has NOT joined yet."""
    client = get_client()
    try:
        result = await client(SearchRequest(q=query, limit=int(limit)))
    except FloodWaitError as exc:
        return [{"_error": f"flood wait {exc.seconds}s"}]

    out: list[dict] = []
    for entity in list(result.chats) + list(result.users):
        username = getattr(entity, "username", None)
        out.append({
            "id": entity.id,
            "name": _name(entity),
            "type": _type(entity),
            "username": username,
            "link": f"https://t.me/{username}" if username else None,
            "participants": getattr(entity, "participants_count", None),
            "verified": bool(getattr(entity, "verified", False)),
        })
    if not out:
        return [{"_note": f"no public results for {query!r}"}]
    return out

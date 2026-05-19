from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import User, Chat, Channel

from app.userbot.client import get_client
from app.tools.read_messages import _query_variants


def _entity_type(entity) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
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


async def search_dialogs(query: str, limit: int = 10) -> list[dict]:
    client = get_client()
    seen: set[int] = set()
    results: list[dict] = []

    for q in _query_variants(query):
        try:
            res = await client(SearchRequest(q=q, limit=limit))
        except Exception:
            continue
        for e in list(res.users) + list(res.chats):
            if e.id in seen:
                continue
            seen.add(e.id)
            results.append({
                "id": e.id,
                "name": _entity_name(e),
                "type": _entity_type(e),
                "username": getattr(e, "username", None),
            })
            if len(results) >= limit * 2:
                break

    return results[:limit]

from telethon.tl.types import User, Chat, Channel

from app.userbot.client import get_client


def _dialog_type(entity) -> str:
    if isinstance(entity, User):
        return "user"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    return "unknown"


def _dialog_name(dialog) -> str:
    return getattr(dialog.entity, "title", None) or (
        " ".join(
            filter(None, [getattr(dialog.entity, "first_name", None),
                          getattr(dialog.entity, "last_name", None)])
        )
    ) or str(dialog.id)


async def search_dialogs(query: str, limit: int = 10) -> list[dict]:
    client = get_client()
    q = query.lower()
    results: list[dict] = []
    async for dialog in client.iter_dialogs():
        name = _dialog_name(dialog)
        if q in name.lower():
            results.append({
                "id": dialog.id,
                "name": name,
                "type": _dialog_type(dialog.entity),
                "unread_count": dialog.unread_count,
            })
            if len(results) >= limit:
                break
    return results

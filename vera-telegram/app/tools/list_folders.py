"""List Telegram folders (Dialog Filters) with chat counts."""
from app.userbot.client import get_client


async def list_folders() -> list[dict]:
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    client = get_client()
    try:
        result = await client(GetDialogFiltersRequest())
    except Exception as exc:
        return [{"_error": f"GetDialogFilters failed: {exc}"}]
    filters_list = getattr(result, "filters", None) or result
    out: list[dict] = []
    for f in filters_list:
        t = getattr(f, "title", None)
        title = getattr(t, "text", t) if t else None
        if not title:
            continue
        peers = (getattr(f, "include_peers", None) or []) + \
                (getattr(f, "pinned_peers", None) or [])
        out.append({
            "title": title,
            "chat_count": len(peers),
            "peer_ids": [int(getattr(p, "user_id", None)
                              or getattr(p, "chat_id", None)
                              or getattr(p, "channel_id", None) or 0)
                          for p in peers],
        })
    return out

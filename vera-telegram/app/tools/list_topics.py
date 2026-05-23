"""List forum topics in a supergroup via MTProto.

Telethon's `GetForumTopicsRequest` was added in a later layer; if our
installed version doesn't expose it we fall back to iter_messages +
deduplication of reply_to.forum_topic IDs (slower, but works).
"""
import logging

from app.userbot.client import get_client
from app.tools.read_messages import _resolve_peer_by_id_or_name

log = logging.getLogger(__name__)

try:
    from telethon.tl.functions.channels import GetForumTopicsRequest  # noqa: F401
    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False
    log.warning("Telethon lacks GetForumTopicsRequest — using fallback scan")


async def list_forum_topics(peer: str, limit: int = 100) -> list[dict]:
    client = get_client()
    entity = await _resolve_peer_by_id_or_name(peer)
    if not entity:
        return [{"error": f"peer not found: {peer}"}]

    if _HAS_NATIVE:
        from telethon.tl.functions.channels import GetForumTopicsRequest
        from telethon.tl.types import ForumTopic
        res = await client(GetForumTopicsRequest(
            channel=entity, offset_date=None, offset_id=0, offset_topic=0,
            limit=limit, q="",
        ))
        topics = []
        for t in getattr(res, "topics", []) or []:
            if not isinstance(t, ForumTopic):
                continue
            topics.append({
                "id": t.id, "title": t.title,
                "top_message": t.top_message,
                "unread_count": getattr(t, "unread_count", 0),
                "closed": bool(getattr(t, "closed", False)),
                "hidden": bool(getattr(t, "hidden", False)),
            })
        return topics

    # Fallback: scan recent messages, collect distinct forum_topic IDs.
    # Less efficient but supported on all Telethon versions.
    seen: dict[int, dict] = {}
    async for msg in client.iter_messages(entity, limit=2000):
        rt = getattr(msg, "reply_to", None)
        if rt is None:
            continue
        tid = getattr(rt, "forum_topic", None) and getattr(rt, "reply_to_top_id", None)
        if not tid:
            tid = getattr(rt, "reply_to_msg_id", None)
        if tid and tid not in seen:
            seen[tid] = {"id": tid, "title": f"topic#{tid}"}
        if len(seen) >= limit:
            break
    return list(seen.values())

"""List forum topics in a supergroup via MTProto."""
import logging

from telethon.tl.functions.channels import GetForumTopicsRequest
from telethon.tl.types import ForumTopic

from app.userbot.client import get_client
from app.tools.read_messages import _resolve_peer_by_id_or_name

log = logging.getLogger(__name__)


async def list_forum_topics(peer: str, limit: int = 100) -> list[dict]:
    client = get_client()
    entity = await _resolve_peer_by_id_or_name(peer)
    if not entity:
        return [{"error": f"peer not found: {peer}"}]
    res = await client(GetForumTopicsRequest(
        channel=entity, offset_date=None, offset_id=0, offset_topic=0,
        limit=limit, q="",
    ))
    topics = []
    for t in getattr(res, "topics", []) or []:
        if not isinstance(t, ForumTopic):
            continue
        topics.append({
            "id": t.id,
            "title": t.title,
            "top_message": t.top_message,
            "unread_count": getattr(t, "unread_count", 0),
            "closed": bool(getattr(t, "closed", False)),
            "hidden": bool(getattr(t, "hidden", False)),
        })
    return topics

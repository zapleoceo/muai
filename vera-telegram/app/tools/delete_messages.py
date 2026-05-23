"""Delete Telegram messages via Telethon userbot.

Two operations:
  - delete_messages(peer, message_ids, revoke): drop specific msgs.
    Own messages always deletable. Others only if caller has admin
    rights with delete_messages permission.
  - clear_history(peer, just_clear, revoke): wipe entire chat history.
    Works for personal chats (just_clear hides from caller; revoke=True
    requires both sides to be on supported versions). For groups/
    channels needs admin.

Both raise ChatAdminRequiredError when permissions are missing — let
the caller surface that to the user instead of swallowing silently.
"""
import logging

from telethon.errors import ChatAdminRequiredError, MessageDeleteForbiddenError
from telethon.tl.functions.messages import DeleteHistoryRequest

from app.userbot.client import get_client
from app.tools.read_messages import _resolve_peer_by_id_or_name

log = logging.getLogger(__name__)


async def delete_messages(peer: str, message_ids: list[int],
                          revoke: bool = True) -> dict:
    client = get_client()
    entity = await _resolve_peer_by_id_or_name(peer)
    if not entity:
        return {"ok": False, "error": f"peer not found: {peer}"}
    try:
        res = await client.delete_messages(entity, message_ids, revoke=revoke)
    except MessageDeleteForbiddenError as exc:
        return {"ok": False, "error": f"forbidden: {exc}"}
    except ChatAdminRequiredError:
        return {"ok": False, "error": "need admin with delete_messages right"}
    total = sum(getattr(r, "pts_count", 0) or 0 for r in res) if res else 0
    return {"ok": True, "requested": len(message_ids), "deleted_pts": total}


async def clear_history(peer: str, just_clear: bool = False,
                        revoke: bool = False, max_id: int = 0) -> dict:
    """Delete the whole chat history with `peer`.

    just_clear=True: only hide from your side, leave for the other.
    revoke=True: try to delete from both sides (works in private chats
                   for recent messages; in groups requires admin).
    max_id=0: from the latest down. Pass message_id to delete only up
              to that point.
    """
    client = get_client()
    entity = await _resolve_peer_by_id_or_name(peer)
    if not entity:
        return {"ok": False, "error": f"peer not found: {peer}"}
    try:
        res = await client(DeleteHistoryRequest(
            peer=entity, max_id=max_id, just_clear=just_clear,
            revoke=revoke,
        ))
    except ChatAdminRequiredError:
        return {"ok": False, "error": "need admin to clear group history"}
    return {"ok": True, "pts_count": getattr(res, "pts_count", 0),
            "offset": getattr(res, "offset", 0)}

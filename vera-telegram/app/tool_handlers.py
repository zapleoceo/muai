import logging
from typing import Any, Callable, Awaitable

from app.tools.search_dialogs import search_dialogs
from app.tools.list_dialogs import list_recent_dialogs
from app.tools.read_messages import read_messages, _resolve_peer_by_id_or_name
from app.tools.send_message import send_message_to
from app.tools.get_dialog_info import get_dialog_info_for
from app.tools.delete_messages import delete_messages, clear_history
from app.tools.list_topics import list_forum_topics
from app.tools.list_folders import list_folders as _list_folders
from app.tools.read_messages_batch import read_messages_batch
from app.tools.folder_digest import folder_digest

log = logging.getLogger(__name__)


async def _t_search(query: str, limit: int = 15) -> Any:
    return await search_dialogs(query, limit=limit)


async def _t_list_recent(limit: int = 15, exclude_channels: bool = False,
                         only_unread: bool = False) -> Any:
    return await list_recent_dialogs(
        limit=int(limit), exclude_channels=bool(exclude_channels),
        only_unread=bool(only_unread),
    )


async def _t_read(chat_id: int = 0, peer: str = "", days: int = 1,
                  limit: int = 50, ocr_images: bool = True) -> Any:
    if not chat_id and not peer:
        raise ValueError("Either chat_id or peer is required")
    return await read_messages(
        peer=str(chat_id) if chat_id else peer,
        limit=limit, offset_days=days, ocr_images=bool(ocr_images),
    )


async def _t_send(chat_id: int = 0, peer: str = "", text: str = "") -> Any:
    if not text:
        raise ValueError("text is required")
    target = str(chat_id) if chat_id else peer
    if not target:
        raise ValueError("chat_id or peer is required")
    return await send_message_to(target, text)


async def _t_info(chat_id: int = 0, peer: str = "") -> Any:
    target = str(chat_id) if chat_id else peer
    if not target:
        raise ValueError("chat_id or peer is required")
    return await get_dialog_info_for(target)


async def _t_delete(chat_id: int = 0, peer: str = "",
                    message_ids: list[int] | None = None,
                    revoke: bool = True) -> Any:
    target = str(chat_id) if chat_id else peer
    if not target:
        raise ValueError("chat_id or peer is required")
    if not message_ids:
        raise ValueError("message_ids list is required")
    return await delete_messages(target, list(message_ids), revoke=bool(revoke))


async def _t_clear_history(chat_id: int = 0, peer: str = "",
                            just_clear: bool = False, revoke: bool = False,
                            max_id: int = 0) -> Any:
    target = str(chat_id) if chat_id else peer
    if not target:
        raise ValueError("chat_id or peer is required")
    return await clear_history(target, just_clear=bool(just_clear),
                                revoke=bool(revoke), max_id=int(max_id))


async def _t_list_topics(chat_id: int = 0, peer: str = "",
                          limit: int = 100) -> Any:
    target = str(chat_id) if chat_id else peer
    if not target:
        raise ValueError("chat_id or peer is required")
    return await list_forum_topics(target, limit=int(limit))


HANDLERS: dict[str, Callable[..., Awaitable[Any]]] = {
    "telegram_list_recent_dialogs": _t_list_recent,
    "telegram_search_dialogs":      _t_search,
    "telegram_read_messages":       _t_read,
    "telegram_send_message":        _t_send,
    "telegram_get_dialog_info":     _t_info,
    "telegram_delete_messages":     _t_delete,
    "telegram_clear_history":       _t_clear_history,
    "telegram_list_forum_topics":   _t_list_topics,
    "telegram_list_folders":        lambda: _list_folders(),
    "telegram_read_messages_batch": lambda chat_ids, days=1, limit_per_chat=30, ocr_images=False:
        read_messages_batch(chat_ids=list(chat_ids), days=int(days),
                             limit_per_chat=int(limit_per_chat),
                             ocr_images=bool(ocr_images)),
    "telegram_folder_digest":       lambda folder_title, days=1, limit_per_chat=50:
        folder_digest(folder_title=str(folder_title), days=int(days),
                       limit_per_chat=int(limit_per_chat)),
}

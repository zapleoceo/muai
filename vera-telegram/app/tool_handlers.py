import logging
from typing import Any, Callable, Awaitable

from app.tools.search_dialogs import search_dialogs
from app.tools.list_dialogs import list_recent_dialogs
from app.tools.read_messages import read_messages, _resolve_peer_by_id_or_name
from app.tools.send_message import send_message_to
from app.tools.get_dialog_info import get_dialog_info_for

log = logging.getLogger(__name__)


async def _t_search(query: str, limit: int = 15) -> Any:
    return await search_dialogs(query, limit=limit)


async def _t_list_recent(limit: int = 15, exclude_channels: bool = False,
                         only_unread: bool = False) -> Any:
    return await list_recent_dialogs(
        limit=int(limit), exclude_channels=bool(exclude_channels),
        only_unread=bool(only_unread),
    )


async def _t_read(chat_id: int = 0, peer: str = "", days: int = 1, limit: int = 50) -> Any:
    if not chat_id and not peer:
        raise ValueError("Either chat_id or peer is required")
    return await read_messages(
        peer=str(chat_id) if chat_id else peer,
        limit=limit, offset_days=days,
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


HANDLERS: dict[str, Callable[..., Awaitable[Any]]] = {
    "telegram_list_recent_dialogs": _t_list_recent,
    "telegram_search_dialogs":      _t_search,
    "telegram_read_messages":       _t_read,
    "telegram_send_message":        _t_send,
    "telegram_get_dialog_info":     _t_info,
}

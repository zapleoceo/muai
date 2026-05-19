import logging
from dataclasses import dataclass

from app.tools.search_dialogs import search_dialogs
from app.tools.read_messages import read_messages
from app.tools.send_message import send_message
from app.tools.get_dialog_info import get_dialog_info

logger = logging.getLogger(__name__)

AGENT_ID = "vera-telegram"
AGENT_NAME = "Telegram Userbot"
CAPABILITIES = ["telegram:read", "telegram:search", "telegram:send"]
REQUIRED_CAPS: list[str] = []
HTTP_URL = "http://vera-telegram:8001"


@dataclass
class ToolResult:
    success: bool
    summary: str
    data: dict | list | None = None
    error: str | None = None


async def handle_task(request: str, intent: dict) -> ToolResult:
    ctx = intent or {}
    action = (ctx.get("action") or _infer_action(request, ctx)).lower()

    try:
        if action in ("search_dialogs", "search"):
            query = str(ctx.get("query") or ctx.get("peer") or request)
            data = await search_dialogs(query, limit=int(ctx.get("limit", 10)))
            return ToolResult(True, f"Найдено диалогов: {len(data)}", data)

        if action in ("send_message", "send"):
            peer = str(ctx.get("peer", ""))
            text = str(ctx.get("text", ""))
            if not peer or not text:
                return ToolResult(False, "send_message: нужны peer и text", error="missing_args")
            data = await send_message(peer, text)
            return ToolResult(True, f"Отправлено в {peer}", data)

        if action in ("get_info", "info"):
            peer = str(ctx.get("peer", ""))
            data = await get_dialog_info(peer)
            return ToolResult(True, f"Инфо о {data.get('name', peer)}", data)

        peer = str(ctx.get("peer", ""))
        limit = int(ctx.get("limit", 30))
        days = int(ctx.get("days", 1))
        data = await read_messages(peer, limit=limit, offset_days=days)
        peer_label = data[0].get("_chat_name", peer) if data else peer
        return ToolResult(
            True,
            f"Прочитано {len(data)} сообщений из «{peer_label}» за {days}д",
            data,
        )

    except LookupError as exc:
        return ToolResult(False, str(exc), error="peer_not_found")
    except Exception as exc:
        logger.exception("Tool error: action=%s ctx=%s", action, ctx)
        return ToolResult(False, f"Ошибка инструмента: {exc}", error=str(exc))


_READ_HINTS = ("прочитай", "о чём", "о чем", "общались", "переписк", "сообщени")
_SEND_HINTS = ("отправь", "напиши ", "send ")
_SEARCH_HINTS = ("найди чат", "найди диалог", "поищи", "search")


def _infer_action(request: str, ctx: dict) -> str:
    r = request.lower()
    if ctx.get("text"):
        return "send_message"
    if any(h in r for h in _SEND_HINTS):
        return "send_message"
    if any(h in r for h in _SEARCH_HINTS):
        return "search_dialogs"
    if any(h in r for h in _READ_HINTS):
        return "read_messages"
    return "read_messages"

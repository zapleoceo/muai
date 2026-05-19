import logging
import re
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
    ctx = dict(intent or {})
    action = (ctx.get("action") or _infer_action(request, ctx)).lower()

    if action in ("read_messages", "read") and not ctx.get("peer"):
        derived = _derive_peer(request)
        if derived:
            ctx["peer"] = derived
        else:
            action = "search_dialogs"
            ctx["query"] = ctx.get("query") or request

    try:
        if action in ("search_dialogs", "search"):
            query = str(ctx.get("query") or ctx.get("peer") or request)
            data = await search_dialogs(query, limit=int(ctx.get("limit", 15)))
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
        limit = int(ctx.get("limit", 50))
        days = int(ctx.get("days", 1))
        data = await read_messages(peer, limit=limit, offset_days=days, request_hint=request)
        chats = data.get("chats_read", [])
        total = data.get("candidates_total", 0)
        if not chats:
            return ToolResult(True, f"Совпадений: {total}, сообщений за {days}д нет", data)
        names = [c["chat_name"] for c in chats]
        total_msgs = sum(c["messages_count"] for c in chats)
        summary = (
            f"Прочитал {total_msgs} сообщений из {len(chats)} чатов "
            f"({', '.join(names)}) за {days}д. Совпадений всего: {total}"
        )
        return ToolResult(True, summary, data)

    except LookupError as exc:
        return ToolResult(False, str(exc), error="peer_not_found")
    except Exception as exc:
        logger.exception("Tool error: action=%s ctx=%s", action, ctx)
        return ToolResult(False, f"Ошибка инструмента: {exc}", error=str(exc))


def _derive_peer(request: str) -> str:
    words = re.findall(r"[A-Za-zА-ЯЁ][A-Za-zА-ЯЁа-яё0-9_]{2,}", request)
    return max(words, key=len) if words else ""


_READ_HINTS = ("прочитай", "о чём", "о чем", "общались", "переписк", "сообщени",
               "анонс", "новост", "что там", "что в чате")
_SEND_HINTS = ("отправь", "напиши ", "send ")
_SEARCH_HINTS = ("найди чат", "найди диалог", "поищи", "search", "список чатов",
                 "все чаты", "в названии")


def _infer_action(request: str, ctx: dict) -> str:
    r = request.lower()
    if ctx.get("text"):
        return "send_message"
    if any(h in r for h in _SEARCH_HINTS):
        return "search_dialogs"
    if any(h in r for h in _SEND_HINTS):
        return "send_message"
    if any(h in r for h in _READ_HINTS):
        return "read_messages"
    return "read_messages"

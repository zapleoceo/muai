import json
import logging
from dataclasses import dataclass

from app.intent import parse_intent
from app.tools.search_dialogs import search_dialogs
from app.tools.read_messages import read_messages
from app.tools.send_message import send_message
from app.tools.get_dialog_info import get_dialog_info

logger = logging.getLogger(__name__)

AGENT_ID = "vera-telegram"
AGENT_NAME = "Telegram Userbot"
CAPABILITIES = ["telegram:read", "telegram:search", "telegram:send"]
REQUIRED_CAPS = ["chat:fast"]
HTTP_URL = "http://vera-telegram:8001"


@dataclass
class TaskResult:
    success: bool
    output: str
    tokens_used: dict | None = None


async def handle_task(input_text: str) -> TaskResult:
    intent = await parse_intent(input_text)
    action = intent.get("action", "read_messages")
    peer = str(intent.get("peer", ""))
    limit = int(intent.get("limit", 20))
    text = str(intent.get("text", ""))
    days = int(intent.get("days", 1))

    try:
        if action == "search_dialogs":
            data = await search_dialogs(peer or input_text, limit=limit)
        elif action == "send_message":
            data = await send_message(peer, text)
        elif action == "get_info":
            data = await get_dialog_info(peer)
        else:
            data = await read_messages(peer, limit=limit, offset_days=days)

        return TaskResult(success=True, output=json.dumps(data, ensure_ascii=False))
    except Exception as exc:
        logger.exception("Tool error for action=%s peer=%s: %s", action, peer, exc)
        return TaskResult(success=False, output=f"Error: {exc}")

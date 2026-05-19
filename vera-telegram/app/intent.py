import json
import logging

from vera_shared.providers.gemini import get_gemini

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an intent parser for a Telegram userbot. "
    "Given a task description, return JSON with keys: "
    "action (search_dialogs|read_messages|send_message|get_info), "
    "peer (string, who to talk to, empty if not applicable), "
    "limit (int, default 20), "
    "text (string, message body for send_message), "
    "days (int, how many days back, default 1). "
    "Return ONLY valid JSON, no markdown."
)


async def parse_intent(task_text: str) -> dict:
    provider = get_gemini()
    messages = [
        {"role": "user", "content": f"{_SYSTEM}\n\nTask: {task_text}"},
    ]
    try:
        text, _, _ = await provider.chat(messages, capability="chat:fast")
        raw = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Intent parse failed: %s", exc)
        return {"action": "read_messages", "peer": "", "limit": 20, "text": "", "days": 1}

import logging

from aiogram.types import Message

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты личный ассистент владельца этого Telegram-аккаунта. "
    "Отвечай лаконично, по делу, на том же языке, на котором написано последнее сообщение. "
    "Не раскрывай, что ты AI, если тебя напрямую не спросят."
)


async def get_dialog_context(chat_id: int, limit: int = 20) -> list[LLMMessage]:
    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        rows = await repo.get_messages(chat_id=chat_id, limit=limit)

    result = []
    for row in rows:
        role = "assistant" if row.direction == "out" else "user"
        content = row.text or row.caption or f"[{row.media_type or 'media'}]"
        result.append(LLMMessage(role=role, content=content))
    return result


async def run_ai_reply(chat_id: int, trigger_msg: Message | None = None) -> str:
    context = await get_dialog_context(chat_id, limit=20)
    if not context:
        return "История диалога пуста — нечего анализировать."

    provider = get_llm_provider()
    reply = await provider.complete(context, system_prompt=SYSTEM_PROMPT)
    return reply

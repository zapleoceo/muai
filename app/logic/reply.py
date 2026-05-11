import logging

from app.bot.storage import get_dialog_context
from app.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты личный ассистент владельца этого Telegram-аккаунта. "
    "Отвечай лаконично, по делу, на том же языке, на котором написано последнее сообщение. "
    "Не раскрывай, что ты AI, если тебя напрямую не спросят."
)


async def run_ai_reply(chat_id: int) -> str:
    context = await get_dialog_context(chat_id, limit=20)
    if not context:
        return "История диалога пуста — нечего анализировать."
    return await get_llm_provider().complete(context, system_prompt=SYSTEM_PROMPT)

import logging

from app.bot.storage import get_dialog_context
from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider
from app.llm.gemini_provider import GeminiContentError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты личный ассистент владельца этого Telegram-аккаунта. "
    "Отвечай лаконично, по делу, на том же языке, на котором написано последнее сообщение. "
    "Не раскрывай, что ты AI, если тебя напрямую не спросят."
)


async def run_ai_reply(chat_id: int, question: str | None = None) -> str:
    context = await get_dialog_context(chat_id, limit=20)
    if not context and not question:
        return "История диалога пуста — нечего анализировать."

    if question:
        context.append(LLMMessage(role="user", content=question))

    provider = get_llm_provider()

    try:
        return await provider.complete(context, system_prompt=SYSTEM_PROMPT)
    except GeminiContentError as exc:
        logger.warning("Gemini blocked full context for chat %d: %s", chat_id, exc.reason)
        # Retry with only the explicit question (no chat history that may have triggered filter)
        if question:
            logger.info("Retrying with question-only context")
            fallback = [LLMMessage(role="user", content=question)]
            return await provider.complete(fallback, system_prompt=SYSTEM_PROMPT)
        raise

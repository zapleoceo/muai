import logging
import re

from app.bot.storage import get_dialog_context, search_chat_context
from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider
from app.llm.gemini_provider import GeminiContentError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты личный ассистент владельца этого Telegram-аккаунта. "
    "У тебя есть доступ к истории переписки из Telegram-чатов владельца. "
    "Когда тебе предоставлена переписка из другого чата — используй её для ответа. "
    "Отвечай лаконично, по делу, на том же языке, на котором написано последнее сообщение. "
    "Не раскрывай, что ты AI, если тебя напрямую не спросят."
)

# Patterns that suggest the user is asking about another chat/person
_CHAT_REF_PATTERNS = [
    r"(?:чат|переписк[аеу]|разговор|диалог)\s+с\s+(\w+)",
    r"с\s+(\w+)\s+(?:говорили|писали|переписывались|общались|обсуждали)",
    r"у\s+(\w+)\s+(?:спроси|узнай|посмотри)",
    r"посмотри\s+чат\s+с\s+(\w+)",
    r"что\s+(?:пишет|написал[аи]?|говорит|сказал[аи]?)\s+(\w+)",
    r"о\s+чём\s+(?:говорили|писали|переписывались)\s+с\s+(\w+)",
]


def _extract_chat_name(text: str) -> str | None:
    """Extract a person/chat name from a question about another chat."""
    for pattern in _CHAT_REF_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


async def run_ai_reply(chat_id: int, question: str | None = None) -> str:
    context = await get_dialog_context(chat_id, limit=20)
    if not context and not question:
        return "История диалога пуста — нечего анализировать."

    extra_context: str | None = None
    if question:
        name = _extract_chat_name(question)
        if name:
            extra_context = await search_chat_context(name)
            if extra_context:
                logger.info("Injecting cross-chat context for '%s' into reply", name)

    messages: list[LLMMessage] = list(context)
    if extra_context:
        # Prepend transcript as a system-style user message so LLM has the data
        messages.insert(0, LLMMessage(role="user", content=extra_context))
        messages.insert(1, LLMMessage(role="assistant", content="Понял, изучил переписку."))
    if question:
        messages.append(LLMMessage(role="user", content=question))

    provider = get_llm_provider()

    try:
        return await provider.complete(messages, system_prompt=SYSTEM_PROMPT)
    except GeminiContentError as exc:
        logger.warning("Gemini blocked full context for chat %d: %s", chat_id, exc.reason)
        if question:
            logger.info("Retrying with question-only context")
            fallback = [LLMMessage(role="user", content=question)]
            return await provider.complete(fallback, system_prompt=SYSTEM_PROMPT)
        raise

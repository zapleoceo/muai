import logging

from app.bot.storage import fetch_chat_transcript, get_dialog_context, resolve_chat_for_question
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


async def run_ai_reply(chat_id: int, question: str | None = None) -> str:
    context = await get_dialog_context(chat_id, limit=20)
    if not context and not question:
        return "История диалога пуста — нечего анализировать."

    provider = get_llm_provider()

    # Ask LLM which other chat (if any) the question is about
    extra_context: str | None = None
    if question:
        try:
            ref_chat_id = await resolve_chat_for_question(question, provider)
            if ref_chat_id and ref_chat_id != chat_id:
                extra_context = await fetch_chat_transcript(ref_chat_id)
                logger.info("Injecting cross-chat context chat_id=%d into reply", ref_chat_id)
        except Exception:
            logger.exception("Failed to resolve cross-chat reference")

    messages: list[LLMMessage] = list(context)
    if extra_context:
        messages.insert(0, LLMMessage(role="user", content=extra_context))
        messages.insert(1, LLMMessage(role="assistant", content="Понял, изучил переписку."))
    if question:
        messages.append(LLMMessage(role="user", content=question))

    try:
        return await provider.complete(messages, system_prompt=SYSTEM_PROMPT)
    except GeminiContentError as exc:
        logger.warning("Gemini blocked full context for chat %d: %s", chat_id, exc.reason)
        if question:
            logger.info("Retrying with question-only context")
            fallback = [LLMMessage(role="user", content=question)]
            return await provider.complete(fallback, system_prompt=SYSTEM_PROMPT)
        raise

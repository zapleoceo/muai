import logging

from app.bot.storage import get_dialog_context
from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.base import LLMMessage
from app.llm.embedding import embed_text
from app.llm.factory import get_llm_provider
from app.llm.gemini_provider import GeminiContentError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты личный ассистент владельца этого Telegram-аккаунта. "
    "У тебя есть доступ к истории переписки из всех Telegram-чатов владельца. "
    "Когда в контексте есть фрагменты переписки — используй их для ответа. "
    "Отвечай лаконично, по делу, на том же языке, на котором написано последнее сообщение. "
    "Не раскрывай, что ты AI, если тебя напрямую не спросят."
)


async def _retrieve_context(question: str) -> str | None:
    """Vector search across all chats. Returns formatted excerpts or None."""
    try:
        q_vec = await embed_text(question, task_type="RETRIEVAL_QUERY")
    except RuntimeError as exc:
        logger.warning("Embedding query failed: %s", exc)
        return None

    async with AsyncSessionLocal() as session:
        chunks = await MessageRepo(session).search_chunks(q_vec, limit=12)

    if not chunks:
        return None

    parts: list[str] = ["[Релевантные фрагменты из Telegram-истории]"]
    for row in chunks:
        parts.append(row.chunk_text)
        parts.append("")  # blank line separator
    return "\n".join(parts)


async def run_ai_reply(chat_id: int, question: str | None = None) -> str:
    context = await get_dialog_context(chat_id, limit=20)
    if not context and not question:
        return "История диалога пуста — нечего анализировать."

    provider = get_llm_provider()
    messages: list[LLMMessage] = list(context)

    if question:
        retrieved = await _retrieve_context(question)
        if retrieved:
            logger.info("Vector search returned context for chat=%d", chat_id)
            messages.insert(0, LLMMessage(role="user", content=retrieved))
            messages.insert(1, LLMMessage(role="assistant", content="Понял, изучил фрагменты переписки."))
        messages.append(LLMMessage(role="user", content=question))

    try:
        return await provider.complete(messages, system_prompt=SYSTEM_PROMPT)
    except GeminiContentError as exc:
        logger.warning("Gemini blocked full context for chat %d: %s", chat_id, exc.reason)
        if question:
            logger.info("Retrying with question-only context")
            return await provider.complete(
                [LLMMessage(role="user", content=question)],
                system_prompt=SYSTEM_PROMPT,
            )
        raise

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
    "Когда в начале диалога есть блок «[Релевантные фрагменты]» — это реальные сообщения "
    "из чатов, используй их как основу для ответа. "
    "Отвечай лаконично, по делу, на том же языке, на котором написан вопрос. "
    "Не раскрывай, что ты AI, если тебя напрямую не спросят."
)

_SIMILARITY_THRESHOLD = 12   # return up to N chunks; pgvector already ranks by cosine


async def _retrieve_context(question: str) -> str | None:
    try:
        q_vec = await embed_text(question, task_type="RETRIEVAL_QUERY")
    except RuntimeError as exc:
        logger.warning("Embedding query failed: %s", exc)
        return None

    async with AsyncSessionLocal() as session:
        chunks = await MessageRepo(session).search_chunks(q_vec, limit=_SIMILARITY_THRESHOLD)

    if not chunks:
        return None

    parts = ["[Релевантные фрагменты из Telegram-истории владельца]"]
    for row in chunks:
        parts.append(row.chunk_text)
    return "\n\n".join(parts)


async def run_ai_reply(chat_id: int, question: str | None = None) -> str:
    context = await get_dialog_context(chat_id, limit=20)
    if not context and not question:
        return "История диалога пуста — нечего анализировать."

    provider = get_llm_provider()
    messages: list[LLMMessage] = []

    # Inject retrieved RAG context as the very first user turn so the LLM
    # sees it before the chat history. The system prompt explains what it is.
    if question:
        retrieved = await _retrieve_context(question)
        if retrieved:
            logger.info("RAG: injecting %d chars of context for chat=%d", len(retrieved), chat_id)
            messages.append(LLMMessage(role="user", content=retrieved))
            messages.append(LLMMessage(role="assistant", content="Принял. Готов отвечать с учётом этой информации."))

    messages.extend(context)

    if question:
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

import json

from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider
from app.services.answering_types import Plan, RetrievedContext


_ANSWER_SYSTEM_PROMPT = (
    "Ты AnswerLLM. Отвечай только на основе RetrievedContext. "
    "Запрещено придумывать факты, которых нет в RetrievedContext. "
    "Если данных недостаточно или контекст пуст — прямо скажи, что данных нет, и что нужно уточнить. "
    "Пиши на языке вопроса. Отвечай лаконично и по делу."
)

_MAX_CONTEXT_CHARS = 12_000
_MAX_MSG_CHARS = 900
_MAX_CHUNK_CHARS = 1_400


def _clip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def format_retrieved_context(ctx: RetrievedContext) -> str:
    parts: list[str] = []

    meta = ctx.meta or {}
    if meta:
        parts.append("[META]\n" + json.dumps(meta, ensure_ascii=False))

    if ctx.stats:
        parts.append("[STATS]\n" + json.dumps(ctx.stats, ensure_ascii=False))

    if ctx.messages:
        lines = []
        for m in ctx.messages:
            ts = m.get("date_utc") or ""
            role = m.get("role") or ""
            chat_id = m.get("chat_id")
            text = _clip(str(m.get("text") or ""), _MAX_MSG_CHARS)
            lines.append(f"{ts} chat={chat_id} {role}: {text}")
        parts.append("[MESSAGES]\n" + "\n".join(lines))

    if ctx.chunks:
        lines = []
        for c in ctx.chunks:
            chat_title = c.get("chat_title") or ""
            rng = ""
            if c.get("msg_date_from") or c.get("msg_date_to"):
                rng = f" ({c.get('msg_date_from') or ''}..{c.get('msg_date_to') or ''})"
            text = _clip(str(c.get("text") or ""), _MAX_CHUNK_CHARS)
            lines.append(f"{chat_title}{rng}\n{text}")
        parts.append("[CHUNKS]\n" + "\n\n".join(lines))

    s = "\n\n".join(parts).strip()
    if len(s) > _MAX_CONTEXT_CHARS:
        s = s[: _MAX_CONTEXT_CHARS - 1].rstrip() + "…"
    return s


async def answer_from_context(*, query: str, plan: Plan, ctx: RetrievedContext) -> str:
    if plan.clarify_question:
        return plan.clarify_question

    context_text = format_retrieved_context(ctx)
    if not context_text:
        return "Нет данных в контексте для ответа. Уточни период/чат/тему, чтобы я мог найти сообщения."

    provider = get_llm_provider()
    messages = [
        LLMMessage(role="user", content="RetrievedContext:\n" + context_text),
        LLMMessage(role="user", content="UserQuery:\n" + query),
    ]
    return await provider.complete(messages, system_prompt=_ANSWER_SYSTEM_PROMPT)

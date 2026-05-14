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

_MAX_CONTEXT_CHARS = 40_000
_MAX_MSG_CHARS = 2_000
_MAX_CHUNK_CHARS = 3_000
_SEGMENT_MAX_CHARS = 9_000
_SEGMENT_MAX_MESSAGES = 80
_MAX_CHAT_SEGMENTS = 10


_HIER_SUMMARY_SYSTEM_PROMPT = (
    "Ты SummarizerLLM. Твоя задача — суммировать большие объемы переписки. "
    "Опирайся только на предоставленные сообщения. "
    "Не выдумывай факты. Если данных мало — так и скажи. "
    "Пиши на языке запроса. "
    "Выход делай компактным, структурированным."
)

_HIER_REDUCE_SYSTEM_PROMPT = (
    "Ты SummarizerLLM. Твоя задача — объединить несколько частичных саммари в одно итоговое. "
    "Не выдумывай факты, опирайся только на входные саммари. "
    "Пиши на языке запроса. "
    "Сохрани ключевые темы, события, решения и выводы."
)


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
            link = m.get("link") or ""
            text = _clip(str(m.get("text") or ""), _MAX_MSG_CHARS)
            if link:
                lines.append(f"{ts} chat={chat_id} {role}: {text}\nlink: {link}")
            else:
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


def _format_messages_for_summary(messages: list[dict]) -> str:
    lines: list[str] = []
    for m in messages:
        ts = m.get("date_utc") or ""
        chat = (m.get("chat") or {}).get("title") or ""
        role = m.get("role") or ""
        text = str(m.get("text") or "")
        if not text:
            continue
        if len(text) > 1200:
            text = text[:1199].rstrip() + "…"
        prefix = f"{ts} {chat} {role}: ".strip()
        lines.append(prefix + text)
    return "\n".join(lines).strip()


def _split_messages_into_segments(messages: list[dict]) -> list[list[dict]]:
    segs: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for m in messages:
        t = str(m.get("text") or "")
        if not t:
            continue
        add = min(len(t), 1200) + 80
        if cur and (len(cur) >= _SEGMENT_MAX_MESSAGES or cur_chars + add > _SEGMENT_MAX_CHARS):
            segs.append(cur)
            if len(segs) >= _MAX_CHAT_SEGMENTS:
                return segs
            cur = []
            cur_chars = 0
        cur.append(m)
        cur_chars += add
    if cur:
        segs.append(cur)
    return segs


async def summarize_large_history(*, query: str, messages: list[dict], language: str = "ru") -> str:
    if not messages:
        return "Нет данных в контексте для ответа. Уточни период/чат/тему, чтобы я мог найти сообщения."

    msgs_sorted = sorted(
        messages,
        key=lambda m: (str(m.get("date_utc") or ""), int(m.get("message_id") or 0)),
    )
    by_chat: dict[int, list[dict]] = {}
    for m in msgs_sorted:
        cid = int(m.get("chat_id") or 0)
        by_chat.setdefault(cid, []).append(m)

    provider = get_llm_provider()
    per_chat_summaries: list[str] = []
    for cid, chat_msgs in by_chat.items():
        segments = _split_messages_into_segments(chat_msgs)
        seg_summaries: list[str] = []
        for i, seg in enumerate(segments, start=1):
            payload = {
                "query": query,
                "language": language,
                "segment": {"index": i, "total": len(segments), "messages": len(seg)},
                "messages": _format_messages_for_summary(seg),
            }
            raw = await provider.complete([LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False))], system_prompt=_HIER_SUMMARY_SYSTEM_PROMPT)
            seg_summaries.append(raw.strip())
        chat_title = (chat_msgs[0].get("chat") or {}).get("title") if chat_msgs else None
        per_chat_summaries.append(
            json.dumps(
                {"chat_id": cid, "chat_title": chat_title, "segments": seg_summaries},
                ensure_ascii=False,
            )
        )

    reduce_payload = {"query": query, "language": language, "chat_summaries": per_chat_summaries}
    final = await provider.complete([LLMMessage(role="user", content=json.dumps(reduce_payload, ensure_ascii=False))], system_prompt=_HIER_REDUCE_SYSTEM_PROMPT)
    return final.strip()


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

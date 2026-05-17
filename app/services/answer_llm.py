import json

from app.llm.base import LLMMessage, LLMProvider
from app.llm.factory import get_llm_provider
from app.services.answering_types import Plan, RetrievedContext
from app.services.style_profile import get_style_profile


_ANSWER_SYSTEM_BASE = (
    "Ты личный AI-ассистент. Отвечай только на основе RetrievedContext. "
    "Запрещено придумывать факты, которых нет в RetrievedContext. "
    "Если данных нет — скажи прямо и коротко. "
    "Пиши на языке вопроса. "
    "Отвечай естественно, как живой человек — без казённых фраз, без избыточного markdown. "
    "Списки и заголовки используй только когда это реально помогает воспринять информацию. "
    "Ссылки на сообщения давай когда они есть в контексте."
)


async def _build_answer_prompt() -> str:
    profile = await get_style_profile()
    if profile:
        return _ANSWER_SYSTEM_BASE + "\n\n" + profile
    return _ANSWER_SYSTEM_BASE

_MAX_CONTEXT_CHARS = 40_000
_MAX_MSG_CHARS = 2_000
_MAX_CHUNK_CHARS = 3_000
_SEGMENT_MAX_CHARS = 9_000
_SEGMENT_MAX_MESSAGES = 80
_MAX_CHAT_SEGMENTS = 10


_HIER_SUMMARY_SYSTEM_PROMPT = (
    "Ты SummarizerLLM. Суммируй переписку опираясь только на предоставленные сообщения. "
    "Не выдумывай факты. Если данных мало — так и скажи. "
    "Пиши на языке запроса. Выход компактный и по делу."
)

_HIER_REDUCE_SYSTEM_PROMPT = (
    "Ты SummarizerLLM. Объедини частичные саммари в одно итоговое. "
    "Только факты из входных данных. Пиши на языке запроса. "
    "Сохрани ключевые темы, события, решения. "
    "Никаких вступлений типа 'На основе предоставленных саммари...' — сразу по делу. "
    "Никаких ненужных заголовков, звёздочек и bold-маркдауна без причины — пиши как живой человек."
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

    dynamic_rows = (ctx.meta or {}).get("dynamic_rows") or []
    if dynamic_rows:
        rows_text = json.dumps(dynamic_rows[:100], ensure_ascii=False)
        parts.append(f"[ROWS count={len(dynamic_rows)}]\n{rows_text}")

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


async def summarize_large_history(*, query: str, messages: list[dict], language: str = "ru", provider: LLMProvider | None = None) -> str:
    if not messages:
        return "данных нет, уточни период или чат"

    msgs_sorted = sorted(
        messages,
        key=lambda m: (str(m.get("date_utc") or ""), int(m.get("message_id") or 0)),
    )
    by_chat: dict[int, list[dict]] = {}
    for m in msgs_sorted:
        cid = int(m.get("chat_id") or 0)
        by_chat.setdefault(cid, []).append(m)

    provider = provider or get_llm_provider()
    style_profile = await get_style_profile()

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

    reduce_prompt = _HIER_REDUCE_SYSTEM_PROMPT
    if style_profile:
        reduce_prompt += "\n\n" + style_profile

    reduce_payload = {"query": query, "language": language, "chat_summaries": per_chat_summaries}
    final = await provider.complete([LLMMessage(role="user", content=json.dumps(reduce_payload, ensure_ascii=False))], system_prompt=reduce_prompt)
    return final.strip()


def _format_chat_list_answer(candidates: list[dict]) -> str:
    seen: set = set()
    unique: list[dict] = []
    for c in candidates:
        cid = c.get("chat_id")
        if cid not in seen:
            seen.add(cid)
            unique.append(c)
    unique.sort(key=lambda x: int(x.get("hit_count") or 0), reverse=True)

    _type_label = {"private": "личный", "group": "группа", "supergroup": "супергруппа", "channel": "канал"}
    lines = [f"Найдено **{len(unique)}** чатов:\n"]
    for c in unique:
        title = c.get("chat_title") or c.get("title") or str(c.get("chat_id"))
        ctype_raw = c.get("chat_type") or c.get("type") or ""
        ctype = _type_label.get(str(ctype_raw), str(ctype_raw))
        hits = int(c.get("hit_count") or 0)
        username = c.get("chat_username") or c.get("username") or ""
        uname_part = f" @{username}" if username else ""
        lines.append(f"- **{title}**{uname_part} ({ctype}) — {hits} упом.")
    return "\n".join(lines)


def _format_dynamic_rows_answer(rows: list[dict]) -> str:
    if not rows:
        return "Запрос выполнен, результатов нет."
    headers = list(rows[0].keys())
    lines = ["| " + " | ".join(str(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows[:200]:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return f"**{len(rows)} строк:**\n\n" + "\n".join(lines)


async def answer_from_context(*, query: str, plan: Plan, ctx: RetrievedContext, provider: LLMProvider | None = None) -> str:
    if plan.clarify_question:
        return plan.clarify_question

    # CHAT_LIST: generate answer directly from structured data, no LLM needed
    chat_candidates = ctx.meta.get("chat_candidates") or []
    if chat_candidates and not ctx.messages and not ctx.chunks:
        return _format_chat_list_answer(chat_candidates)

    context_text = format_retrieved_context(ctx)
    if not context_text:
        return "Нет данных в контексте для ответа. Уточни период/чат/тему, чтобы я мог найти сообщения."

    provider = provider or get_llm_provider()
    system_prompt = await _build_answer_prompt()
    messages = [
        LLMMessage(role="user", content="RetrievedContext:\n" + context_text),
        LLMMessage(role="user", content="UserQuery:\n" + query),
    ]
    return await provider.complete(messages, system_prompt=system_prompt)

import json

from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider
from app.services.answering_types import Plan
from app.services.router_llm.prompts import GRADER_SYSTEM_PROMPT, RERANK_SYSTEM_PROMPT, router_tool_catalog
from app.services.router_llm.router_utils import extract_json


async def rerank_context(
    *,
    query: str,
    candidate_messages: list[dict],
    candidate_chunks: list[dict],
    keep_messages: int = 12,
    keep_chunks: int = 8,
    language: str = "ru",
) -> tuple[dict, str]:
    provider = get_llm_provider()
    msgs = [
        {
            "message_id": m.get("message_id"),
            "chat_id": m.get("chat_id"),
            "chat_title": (m.get("chat") or {}).get("title"),
            "date_utc": m.get("date_utc"),
            "text": str(m.get("text") or "")[:800],
            "link": m.get("link"),
            "score": m.get("score"),
        }
        for m in candidate_messages[:80]
    ]
    chs = [
        {
            "chunk_id": c.get("chunk_id"),
            "chat_id": c.get("chat_id"),
            "chat_title": c.get("chat_title"),
            "msg_date_from": c.get("msg_date_from"),
            "msg_date_to": c.get("msg_date_to"),
            "text": str(c.get("text") or "")[:1000],
            "link": c.get("link"),
        }
        for c in candidate_chunks[:60]
    ]
    input_block = {
        "query": query,
        "limits": {"keep_messages": int(keep_messages), "keep_chunks": int(keep_chunks)},
        "messages": msgs,
        "chunks": chs,
        "language": language,
    }
    messages = [LLMMessage(role="user", content=json.dumps(input_block, ensure_ascii=False))]
    raw = await provider.complete(messages, system_prompt=RERANK_SYSTEM_PROMPT)
    return extract_json(raw), raw


async def grade_context(
    *,
    query: str,
    plan: Plan,
    retrieved_summary: dict,
    language: str = "ru",
) -> tuple[dict, str]:
    msg_n = int(retrieved_summary.get("messages") or 0)
    chunk_n = int(retrieved_summary.get("chunks") or 0)
    total = msg_n + chunk_n

    if msg_n >= 8 or chunk_n >= 4:
        decision = {
            "verdict": "OK",
            "reason": "enough_context",
            "clarify_question": None,
            "router_hint": None,
            "expand_time_range_to": None,
            "propose_dynamic_tool": None,
        }
        return decision, json.dumps(decision, ensure_ascii=False)

    tr = str(plan.time_range.value)
    if total == 0 and tr == "LAST_7_DAYS":
        decision = {
            "verdict": "RETRY",
            "reason": "empty_context_expand_time_range",
            "clarify_question": None,
            "router_hint": "expand_time_range",
            "expand_time_range_to": "LAST_30_DAYS",
            "propose_dynamic_tool": None,
        }
        return decision, json.dumps(decision, ensure_ascii=False)

    if total == 0 and tr == "LAST_30_DAYS":
        decision = {
            "verdict": "RETRY",
            "reason": "empty_context_expand_time_range",
            "clarify_question": None,
            "router_hint": "expand_time_range",
            "expand_time_range_to": "ALL_TIME",
            "propose_dynamic_tool": None,
        }
        return decision, json.dumps(decision, ensure_ascii=False)

    # For chat-specific summary with very few messages — expand to get full history
    _has_chat_query = any(
        tc.get("name") in ("sql_messages_by_chat_query_and_date", "sql_recent_messages_by_chat_query")
        for tc in (plan.model_dump().get("tools") or [])
    )
    if total <= 2 and _has_chat_query and tr not in ("ALL_TIME", "NONE"):
        decision = {
            "verdict": "RETRY",
            "reason": "too_few_messages_for_chat_summary",
            "clarify_question": None,
            "router_hint": "expand_time_range for chat-specific query",
            "expand_time_range_to": "ALL_TIME",
            "propose_dynamic_tool": None,
        }
        return decision, json.dumps(decision, ensure_ascii=False)

    provider = get_llm_provider()
    input_block = {
        "query": query,
        "plan": plan.model_dump(),
        "retrieved_summary": retrieved_summary,
        "catalog": router_tool_catalog(),
        "language": language,
    }
    messages = [LLMMessage(role="user", content=json.dumps(input_block, ensure_ascii=False))]
    raw = await provider.complete(messages, system_prompt=GRADER_SYSTEM_PROMPT)
    return extract_json(raw), raw

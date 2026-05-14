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

from app.services.answer_llm import answer_from_context
from app.services.answering_types import ReplyResult
from app.services.interactions import create_interaction
from app.services.plan_executor import execute_plan
from app.services.router_llm import route_query


async def run_answer_pipeline(
    *,
    chat_id: int,
    user_id: int | None,
    query: str,
    language: str = "ru",
    timezone_name: str = "UTC",
) -> ReplyResult:
    plan, router_raw = await route_query(
        query=query,
        user_id=user_id,
        chat_id=chat_id,
        language=language,
        timezone=timezone_name,
    )

    retrieved = await execute_plan(plan=plan, chat_id=chat_id, query=query, timezone_name=timezone_name)
    text = await answer_from_context(query=query, plan=plan, ctx=retrieved)

    summary = {
        "messages": len(retrieved.messages),
        "chunks": len(retrieved.chunks),
        "stats": retrieved.stats,
        "meta": retrieved.meta,
    }

    interaction_id = await create_interaction(
        user_id=user_id,
        chat_id=chat_id,
        query=query,
        router_plan=plan.model_dump(),
        router_raw=router_raw,
        tool_runs=[tr.model_dump() for tr in retrieved.tool_runs],
        retrieved_summary=summary,
        answer_text=text[:4000] if text else None,
    )

    return ReplyResult(text=text, interaction_id=interaction_id, plan=plan, retrieved=retrieved)

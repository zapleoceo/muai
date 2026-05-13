from app.services.answer_llm import answer_from_context
from app.services.answering_types import ReplyResult
from app.services.interactions import create_interaction
from app.services.plan_executor import execute_plan
from app.services.router_llm import grade_context, route_query


async def run_answer_pipeline(
    *,
    chat_id: int,
    user_id: int | None,
    query: str,
    language: str = "ru",
    timezone_name: str = "UTC",
) -> ReplyResult:
    router_attempts: list[dict] = []
    grade_attempts: list[dict] = []

    plan, router_raw = await route_query(query=query, user_id=user_id, chat_id=chat_id, language=language, timezone=timezone_name)
    router_attempts.append({"raw": router_raw, "plan": plan.model_dump()})

    retrieved = None
    final_plan = plan
    final_router_raw = router_raw

    step = 0
    while True:
        retrieved = await execute_plan(plan=final_plan, chat_id=chat_id, query=query, timezone_name=timezone_name)

        summary = {
            "messages": len(retrieved.messages),
            "chunks": len(retrieved.chunks),
            "stats": retrieved.stats,
            "meta": retrieved.meta,
            "tool_runs": [tr.model_dump() for tr in retrieved.tool_runs],
        }

        if final_plan.clarify_question:
            break

        max_steps = max(1, int(final_plan.max_steps or 1))
        if step >= max_steps - 1:
            break

        if str(final_plan.on_empty.value) != "RETRY":
            break

        decision, grade_raw = await grade_context(query=query, plan=final_plan, retrieved_summary=summary, language=language)
        grade_attempts.append({"raw": grade_raw, "decision": decision})

        verdict = str(decision.get("verdict") or "").upper()
        if verdict == "OK":
            break
        if verdict == "CLARIFY":
            cq = str(decision.get("clarify_question") or "").strip()
            if cq:
                final_plan = final_plan.model_copy(update={"clarify_question": cq, "max_steps": 1})
            break
        if verdict != "RETRY":
            break

        state = {
            "previous_plan": final_plan.model_dump(),
            "retrieved_summary": summary,
            "grade": decision,
        }
        final_plan, final_router_raw = await route_query(
            query=query,
            user_id=user_id,
            chat_id=chat_id,
            language=language,
            timezone=timezone_name,
            state=state,
        )
        router_attempts.append({"raw": final_router_raw, "plan": final_plan.model_dump()})
        step += 1
        continue

    if retrieved is None:
        retrieved = await execute_plan(plan=final_plan, chat_id=chat_id, query=query, timezone_name=timezone_name)

    text = await answer_from_context(query=query, plan=final_plan, ctx=retrieved)

    final_summary = {
        "messages": len(retrieved.messages),
        "chunks": len(retrieved.chunks),
        "stats": retrieved.stats,
        "meta": retrieved.meta,
        "router_attempts": router_attempts,
        "grade_attempts": grade_attempts,
    }

    interaction_id = await create_interaction(
        user_id=user_id,
        chat_id=chat_id,
        query=query,
        router_plan=final_plan.model_dump(),
        router_raw=final_router_raw,
        tool_runs=[tr.model_dump() for tr in retrieved.tool_runs],
        retrieved_summary=final_summary,
        answer_text=text[:4000] if text else None,
    )

    return ReplyResult(text=text, interaction_id=interaction_id, plan=final_plan, retrieved=retrieved)

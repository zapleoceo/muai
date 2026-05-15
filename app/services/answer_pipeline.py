import json
from collections.abc import Awaitable, Callable

from app.services.answer_llm import answer_from_context, summarize_large_history
from app.services.answering_types import (
    DynamicToolSpec,
    PlanOnEmpty,
    PlanStrategy,
    PlanToolCall,
    PlanTimeRange,
    QueryConstraints,
    QueryModel,
    QueryOperation,
    QueryOutputShape,
    ReplyResult,
)
from app.services.interactions import create_interaction
from app.services.plan_executor import execute_plan, tool_get_recent_dialog
from app.services.router_llm import compile_query_model_to_plan, grade_context, rerank_context, route_query


async def run_answer_pipeline(
    *,
    chat_id: int,
    user_id: int | None,
    query: str,
    language: str = "ru",
    timezone_name: str = "UTC",
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> ReplyResult:
    router_attempts: list[dict] = []
    grade_attempts: list[dict] = []

    recent_dialog, _ = await tool_get_recent_dialog(chat_id=chat_id, limit=12)
    init_state = {"recent_dialog": recent_dialog}

    plan, router_raw = await route_query(
        query=query,
        user_id=user_id,
        chat_id=chat_id,
        language=language,
        timezone=timezone_name,
        state=init_state,
    )
    router_attempts.append({"raw": router_raw, "plan": plan.model_dump()})

    retrieved = None
    final_plan = plan
    final_router_raw = router_raw

    async def _progress(text: str) -> None:
        if on_progress is not None:
            await on_progress(text)

    step = 0
    _HARD_LIMIT = 10  # absolute cap regardless of max_steps or grader verdicts
    while step < _HARD_LIMIT:
        if step == 0:
            await _progress("🔍 ищу в базе…")
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

        if (len(retrieved.messages) + len(retrieved.chunks)) == 0 and final_plan.strategy == PlanStrategy.RAG_SEMANTIC:
            await _progress("🔍 ищу в базе…")
            use_time_range = final_plan.time_range != PlanTimeRange.NONE
            base_tools = [t for t in (final_plan.tools or []) if t.name == "get_recent_dialog"] or [PlanToolCall(name="get_recent_dialog", args={"limit": 20})]
            lex = PlanToolCall(
                name="sql_lex_search_messages",
                args={
                    "scope": final_plan.scope.value,
                    "chat_types": [ct.value for ct in (final_plan.chat_types or [])] or None,
                    "chat_ids": final_plan.chat_ids,
                    "query": query,
                    "limit": 60,
                    "use_time_range": bool(use_time_range),
                },
            )
            final_plan = final_plan.model_copy(
                update={
                    "strategy": PlanStrategy.SQL_DATE_SUMMARY,
                    "tools": base_tools + [lex],
                    "max_steps": 2,
                    "on_empty": PlanOnEmpty.RETRY,
                    "notes": "rag_fallback:lex_sql",
                }
            )
            step += 1
            continue

        _has_meta_results = bool(
            retrieved.meta.get("chat_candidates")
            or retrieved.meta.get("dynamic_rows")
            or retrieved.meta.get("active_chats")
        )
        if (len(retrieved.messages) + len(retrieved.chunks)) == 0 and not _has_meta_results and final_plan.time_range != PlanTimeRange.NONE:
            await _progress("📊 анализирую…")
            coverage_plan = final_plan.model_copy(
                update={
                    "strategy": PlanStrategy.SQL_DATE_SUMMARY,
                    "tools": [
                        PlanToolCall(
                            name="sql_stats_by_date",
                            args={
                                "scope": final_plan.scope.value,
                                "chat_types": [ct.value for ct in (final_plan.chat_types or [])] or None,
                                "chat_ids": final_plan.chat_ids,
                            },
                        ),
                        PlanToolCall(
                            name="sql_active_chats_by_date",
                            args={
                                "scope": final_plan.scope.value,
                                "limit": 5,
                                "chat_types": [ct.value for ct in (final_plan.chat_types or [])] or None,
                                "chat_ids": final_plan.chat_ids,
                            },
                        ),
                    ],
                    "max_steps": 1,
                    "on_empty": PlanOnEmpty.ASK_CLARIFY,
                    "notes": "coverage_check",
                }
            )
            coverage_ctx = await execute_plan(plan=coverage_plan, chat_id=chat_id, query=query, timezone_name=timezone_name)
            summary["coverage"] = {"stats": coverage_ctx.stats, "meta": coverage_ctx.meta}

        await _progress("📊 анализирую…")
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

        propose_dynamic = decision.get("propose_dynamic_tool")
        if propose_dynamic and isinstance(propose_dynamic, dict):
            try:
                spec = DynamicToolSpec.model_validate(propose_dynamic)
                if spec.limit > 20:
                    spec = spec.model_copy(update={"limit": 20})
                c = QueryConstraints(
                    scope=final_plan.scope,
                    chat_types=final_plan.chat_types,
                    chat_ids=final_plan.chat_ids,
                    time_range=final_plan.time_range,
                    explicit_from=final_plan.explicit_from,
                    explicit_to=final_plan.explicit_to,
                    limit=spec.limit,
                )
                qm = QueryModel(
                    output_shape=QueryOutputShape.ANALYTICS,
                    operation=QueryOperation.DYNAMIC_QUERY,
                    need_proof=False,
                    constraints=c,
                    dynamic_tool=spec,
                    max_steps=2,
                    on_empty=PlanOnEmpty.RETRY,
                    notes="grade:proposed_dynamic_tool",
                )
                final_plan = compile_query_model_to_plan(query_model=qm, query=query)
                final_router_raw = json.dumps(qm.model_dump(), ensure_ascii=False)
                router_attempts.append({"raw": final_router_raw, "plan": final_plan.model_dump()})
                step += 1
                continue
            except Exception:
                pass

        expand_to = decision.get("expand_time_range_to")
        force_time_range = None
        if isinstance(expand_to, str) and expand_to:
            force_time_range = expand_to

        state = {
            "recent_dialog": recent_dialog,
            "previous_plan": final_plan.model_dump(),
            "retrieved_summary": summary,
            "grade": decision,
        }
        if force_time_range:
            state["force_time_range"] = force_time_range
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

    use_hier_summary = bool(final_plan.time_range.value == "ALL_TIME" and len(retrieved.messages) > 120)
    if not use_hier_summary and (len(retrieved.messages) > 35 or len(retrieved.chunks) > 25):
        decision, rerank_raw = await rerank_context(
            query=query,
            candidate_messages=retrieved.messages,
            candidate_chunks=retrieved.chunks,
            keep_messages=14,
            keep_chunks=10,
            language=language,
        )
        keep_message_ids = {int(x) for x in (decision.get("keep_message_ids") or []) if str(x).isdigit()}
        keep_chunk_ids = {int(x) for x in (decision.get("keep_chunk_ids") or []) if str(x).isdigit()}
        retrieved.meta["rerank"] = {"raw": rerank_raw, "decision": decision}
        if keep_message_ids or keep_chunk_ids:
            if keep_message_ids:
                retrieved.messages = [m for m in retrieved.messages if int(m.get("message_id") or 0) in keep_message_ids]
            if keep_chunk_ids:
                retrieved.chunks = [c for c in retrieved.chunks if int(c.get("chunk_id") or 0) in keep_chunk_ids]
        else:
            retrieved.messages = retrieved.messages[:14]
            retrieved.chunks = retrieved.chunks[:10]

    if use_hier_summary:
        await _progress("✍️ формирую ответ…")
        text = await summarize_large_history(query=query, messages=retrieved.messages, language=language)
    else:
        await _progress("✍️ формирую ответ…")
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

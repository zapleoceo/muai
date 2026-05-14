from app.services.answering_types import (
    Plan,
    PlanChatType,
    PlanOnEmpty,
    PlanScope,
    PlanStrategy,
    PlanTimeRange,
    PlanToolCall,
    QueryModel,
    QueryOperation,
    QueryOutputShape,
)


def validate_plan_invariants(plan: Plan) -> None:
    tool_names = [t.name for t in plan.tools]

    if plan.strategy.value == "INFO_ONLY":
        bad = [n for n in tool_names if n != "get_recent_dialog"]
        if bad:
            raise ValueError("INFO_ONLY допускает только get_recent_dialog")
        if plan.max_steps != 1:
            raise ValueError("INFO_ONLY: max_steps должен быть 1")
        if plan.on_empty.value != "ASK_CLARIFY":
            raise ValueError("INFO_ONLY: on_empty должен быть 'ASK_CLARIFY'")

    if plan.strategy.value == "RAG_SEMANTIC":
        if "rag_search" not in tool_names:
            raise ValueError("RAG_SEMANTIC требует rag_search")
        if plan.max_steps < 2:
            raise ValueError("RAG_SEMANTIC: max_steps должен быть >= 2")
        if plan.on_empty.value != "RETRY":
            raise ValueError("RAG_SEMANTIC: on_empty должен быть 'RETRY'")

    _SQL_TOOLS = {
        "sql_messages_by_date", "sql_stats_by_date", "sql_search_messages_by_date",
        "sql_lex_search_messages", "sql_message_by_tg_ref", "sql_recent_messages_by_chat_query",
        "sql_media_messages_by_chat_query", "sql_dynamic_query",
        "sql_messages_by_chat_query_and_date", "sql_messages_by_folder_and_date",
    }

    if plan.strategy.value == "SQL_DATE_SUMMARY":
        if not any(n in tool_names for n in _SQL_TOOLS):
            raise ValueError("SQL_DATE_SUMMARY требует SQL tool (messages/stats)")
        if plan.max_steps < 2:
            raise ValueError("SQL_DATE_SUMMARY: max_steps должен быть >= 2")
        if plan.on_empty.value != "RETRY":
            raise ValueError("SQL_DATE_SUMMARY: on_empty должен быть 'RETRY'")

    if plan.strategy.value == "HYBRID":
        if "rag_search" not in tool_names:
            raise ValueError("HYBRID требует rag_search")
        if not any(n in tool_names for n in _SQL_TOOLS):
            raise ValueError("HYBRID требует SQL tool (messages/stats)")
        if plan.max_steps < 2:
            raise ValueError("HYBRID: max_steps должен быть >= 2")
        if plan.on_empty.value != "RETRY":
            raise ValueError("HYBRID: on_empty должен быть 'RETRY'")

    if plan.strategy.value == "COMMAND":
        if plan.tools:
            raise ValueError("COMMAND: инструменты должны быть пустыми")
        if plan.max_steps != 1:
            raise ValueError("COMMAND: max_steps должен быть 1")
        if plan.on_empty.value != "ASK_CLARIFY":
            raise ValueError("COMMAND: on_empty должен быть 'ASK_CLARIFY'")


def compile_query_model_to_plan(*, query_model: QueryModel, query: str) -> Plan:
    c = query_model.constraints

    if query_model.clarify_question:
        plan = Plan(
            strategy=PlanStrategy.INFO_ONLY,
            tools=[PlanToolCall(name="get_recent_dialog", args={"limit": 20})],
            time_range=PlanTimeRange.NONE, scope=PlanScope.CURRENT_CHAT,
            chat_types=None, chat_ids=None, explicit_from=None, explicit_to=None,
            clarify_question=query_model.clarify_question, max_steps=1,
            on_empty=PlanOnEmpty.ASK_CLARIFY, notes="compiled:clarify",
        )
        validate_plan_invariants(plan)
        return plan

    chat_types = [PlanChatType(x) for x in c.chat_types] if c.chat_types else None
    base_tools: list[PlanToolCall] = [PlanToolCall(name="get_recent_dialog", args={"limit": 20})]
    time_range = c.time_range
    explicit_from = c.explicit_from
    explicit_to = c.explicit_to

    if query_model.operation == QueryOperation.RECENT_MESSAGES:
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools + [PlanToolCall(
                name="sql_recent_messages_by_chat_query",
                args={"scope": c.scope.value, "chat_query": str(c.chat_query or ""), "limit": int(c.limit or 5), "chat_types": [ct.value for ct in (chat_types or [])] or None},
            )],
            time_range=PlanTimeRange.NONE, scope=c.scope, chat_types=chat_types, chat_ids=c.chat_ids,
            explicit_from=None, explicit_to=None, clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)), on_empty=PlanOnEmpty.RETRY, notes="compiled:recent_messages",
        )
        validate_plan_invariants(plan)
        return plan

    if query_model.operation == QueryOperation.MEDIA_MESSAGES:
        use_time_range = time_range.value != "NONE"
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools + [PlanToolCall(
                name="sql_media_messages_by_chat_query",
                args={"scope": c.scope.value, "chat_query": c.chat_query, "media_type": str(c.media_type or ""), "limit": int(c.limit or 30), "chat_types": [ct.value for ct in (chat_types or [])] or None, "use_time_range": use_time_range},
            )],
            time_range=time_range, scope=c.scope, chat_types=chat_types, chat_ids=c.chat_ids,
            explicit_from=explicit_from, explicit_to=explicit_to, clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)), on_empty=PlanOnEmpty.RETRY, notes="compiled:media_messages",
        )
        validate_plan_invariants(plan)
        return plan

    if query_model.operation == QueryOperation.DYNAMIC_QUERY:
        if query_model.dynamic_tool is None:
            raise ValueError("dynamic_tool required")
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools + [PlanToolCall(
                name="sql_dynamic_query",
                args={"scope": c.scope.value, "chat_types": [ct.value for ct in (chat_types or [])] or None, "chat_ids": c.chat_ids, "spec": query_model.dynamic_tool.model_dump()},
            )],
            time_range=time_range, scope=c.scope, chat_types=chat_types, chat_ids=c.chat_ids,
            explicit_from=explicit_from, explicit_to=explicit_to, clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)), on_empty=PlanOnEmpty.RETRY, notes="compiled:dynamic_query",
        )
        validate_plan_invariants(plan)
        return plan

    if query_model.output_shape == QueryOutputShape.SUMMARY:
        tr = time_range if time_range.value != "NONE" else PlanTimeRange.LAST_7_DAYS
        if time_range.value == "NONE":
            explicit_from = explicit_to = None
        if c.folder:
            main_tool = PlanToolCall(name="sql_messages_by_folder_and_date", args={"scope": c.scope.value, "max_rows": 2000, "folder": str(c.folder), "chat_types": [ct.value for ct in (chat_types or [])] or None})
        elif c.chat_query:
            main_tool = PlanToolCall(name="sql_messages_by_chat_query_and_date", args={"scope": c.scope.value, "max_rows": 2000, "chat_query": str(c.chat_query), "chat_types": [ct.value for ct in (chat_types or [])] or None})
        else:
            main_tool = PlanToolCall(name="sql_messages_by_date", args={"scope": c.scope.value, "max_rows": 2000, "chat_types": [ct.value for ct in (chat_types or [])] or None, "chat_ids": c.chat_ids})
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools + [main_tool, PlanToolCall(name="sql_stats_by_date", args={"scope": c.scope.value, "chat_types": [ct.value for ct in (chat_types or [])] or None, "chat_ids": c.chat_ids})],
            time_range=tr, scope=c.scope, chat_types=chat_types, chat_ids=c.chat_ids,
            explicit_from=explicit_from, explicit_to=explicit_to, clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)), on_empty=PlanOnEmpty.RETRY, notes="compiled:summary",
        )
        validate_plan_invariants(plan)
        return plan

    if query_model.output_shape == QueryOutputShape.LIST:
        lim = int(c.limit or 50)
        variants = [v for v in (query_model.query_variants or []) if str(v).strip()][:4] or [str(query).strip()]
        use_time_range = time_range.value != "NONE"
        tools = list(base_tools)
        if c.chat_query and not c.chat_ids:
            tools.append(PlanToolCall(name="sql_find_chats", args={"query": c.chat_query, "limit": 5}))
        for v in variants:
            tools.append(PlanToolCall(
                name="sql_lex_search_messages",
                args={"scope": c.scope.value, "query": v, "limit": lim,
                      "chat_types": [ct.value for ct in (chat_types or [])] or None,
                      "chat_ids": c.chat_ids,
                      "chat_query": c.chat_query if c.chat_query and not c.chat_ids else None,
                      "use_time_range": use_time_range},
            ))
        if bool(query_model.need_proof):
            tools.append(PlanToolCall(
                name="rag_search",
                args={"scope": c.scope.value, "query": str(query), "top_k": 10, "chat_ids": c.chat_ids},
            ))
        strategy = PlanStrategy.HYBRID if bool(query_model.need_proof) else PlanStrategy.SQL_DATE_SUMMARY
        plan = Plan(
            strategy=strategy, tools=tools,
            time_range=time_range, scope=c.scope, chat_types=chat_types, chat_ids=c.chat_ids,
            explicit_from=explicit_from, explicit_to=explicit_to, clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)), on_empty=PlanOnEmpty.RETRY, notes="compiled:list",
        )
        validate_plan_invariants(plan)
        return plan

    if query_model.output_shape == QueryOutputShape.ANALYTICS:
        tr = time_range if time_range.value != "NONE" else PlanTimeRange.LAST_7_DAYS
        if time_range.value == "NONE":
            explicit_from = explicit_to = None
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools + [
                PlanToolCall(name="sql_active_chats_by_date", args={"scope": c.scope.value, "limit": int(c.limit or 50), "chat_types": [ct.value for ct in (chat_types or [])] or None, "chat_ids": c.chat_ids}),
                PlanToolCall(name="sql_stats_by_date", args={"scope": c.scope.value, "chat_types": [ct.value for ct in (chat_types or [])] or None, "chat_ids": c.chat_ids}),
            ],
            time_range=tr, scope=c.scope, chat_types=chat_types, chat_ids=c.chat_ids,
            explicit_from=explicit_from, explicit_to=explicit_to, clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)), on_empty=PlanOnEmpty.RETRY, notes="compiled:analytics",
        )
        validate_plan_invariants(plan)
        return plan

    # ANSWER with proof → HYBRID, without → RAG
    if bool(query_model.need_proof):
        variants = [v for v in (query_model.query_variants or []) if str(v).strip()][:3] or [str(query).strip()]
        use_time_range = time_range.value != "NONE"
        tools = list(base_tools)
        if c.chat_query and not c.chat_ids:
            tools.append(PlanToolCall(name="sql_find_chats", args={"query": c.chat_query, "limit": 5}))
        for v in variants:
            tools.append(PlanToolCall(name="sql_lex_search_messages", args={"scope": c.scope.value, "query": v, "limit": 60, "chat_types": [ct.value for ct in (chat_types or [])] or None, "chat_ids": c.chat_ids, "chat_query": c.chat_query if c.chat_query and not c.chat_ids else None, "use_time_range": use_time_range}))
        tools.append(PlanToolCall(name="rag_search", args={"scope": c.scope.value, "query": str(query), "top_k": 10, "chat_ids": c.chat_ids}))
        plan = Plan(
            strategy=PlanStrategy.HYBRID, tools=tools,
            time_range=time_range, scope=c.scope, chat_types=chat_types, chat_ids=c.chat_ids,
            explicit_from=explicit_from, explicit_to=explicit_to, clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)), on_empty=PlanOnEmpty.RETRY, notes="compiled:hybrid",
        )
        validate_plan_invariants(plan)
        return plan

    # RAG — if chat_query set without variants, add find_chats so LLM knows if chat exists
    rag_tools = list(base_tools)
    if c.chat_query and not c.chat_ids:
        rag_tools.append(PlanToolCall(name="sql_find_chats", args={"query": c.chat_query, "limit": 5}))
    rag_tools.append(PlanToolCall(name="rag_search", args={"scope": c.scope.value, "query": str(query), "top_k": 12, "chat_ids": c.chat_ids}))
    plan = Plan(
        strategy=PlanStrategy.RAG_SEMANTIC,
        tools=rag_tools,
        time_range=time_range, scope=c.scope, chat_types=chat_types, chat_ids=c.chat_ids,
        explicit_from=explicit_from, explicit_to=explicit_to, clarify_question=None,
        max_steps=max(2, int(query_model.max_steps or 2)), on_empty=PlanOnEmpty.RETRY, notes="compiled:rag",
    )
    validate_plan_invariants(plan)
    return plan

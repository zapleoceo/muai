from app.services.answering_types import (
    DynamicSelect,
    DynamicSelectAgg,
    DynamicToolSpec,
    PlanChatType,
    PlanScope,
    PlanStrategy,
    PlanTimeRange,
    QueryConstraints,
    QueryModel,
    QueryOperation,
    QueryOutputShape,
    PlanOnEmpty,
)
from app.services.router_llm.compiler import compile_query_model_to_plan


def _tool_names(plan):
    return [t.name for t in plan.tools]


def _qm(**kw) -> QueryModel:
    constraints_kw = {k: v for k, v in kw.items() if k in QueryConstraints.model_fields}
    qm_kw = {k: v for k, v in kw.items() if k not in QueryConstraints.model_fields}
    if constraints_kw:
        qm_kw["constraints"] = QueryConstraints(**constraints_kw)
    return QueryModel(**qm_kw)


# 1. clarify → INFO_ONLY
def test_clarify_returns_info_only():
    qm = _qm(clarify_question="What do you mean?")
    plan = compile_query_model_to_plan(query_model=qm, query="?")
    assert plan.strategy == PlanStrategy.INFO_ONLY


# 2. SUMMARY + NONE time → LAST_7_DAYS
def test_summary_none_time_becomes_last_7_days():
    qm = _qm(output_shape=QueryOutputShape.SUMMARY, time_range=PlanTimeRange.NONE)
    plan = compile_query_model_to_plan(query_model=qm, query="summary")
    assert plan.time_range == PlanTimeRange.LAST_7_DAYS


# 3. SUMMARY + YESTERDAY → YESTERDAY + sql_messages_by_date
def test_summary_yesterday_uses_messages_by_date():
    qm = _qm(output_shape=QueryOutputShape.SUMMARY, time_range=PlanTimeRange.YESTERDAY)
    plan = compile_query_model_to_plan(query_model=qm, query="summary")
    assert plan.time_range == PlanTimeRange.YESTERDAY
    assert "sql_messages_by_date" in _tool_names(plan)


# 4. SUMMARY + chat_query → sql_messages_by_chat_query_and_date
def test_summary_with_chat_query_uses_chat_query_and_date():
    qm = _qm(output_shape=QueryOutputShape.SUMMARY, time_range=PlanTimeRange.YESTERDAY, chat_query="Alice")
    plan = compile_query_model_to_plan(query_model=qm, query="summary")
    assert "sql_messages_by_chat_query_and_date" in _tool_names(plan)


# 5. SUMMARY + folder → sql_messages_by_folder_and_date
def test_summary_with_folder_uses_folder_and_date():
    qm = _qm(output_shape=QueryOutputShape.SUMMARY, time_range=PlanTimeRange.YESTERDAY, folder="Work")
    plan = compile_query_model_to_plan(query_model=qm, query="summary")
    assert "sql_messages_by_folder_and_date" in _tool_names(plan)


# 6. ANALYTICS + NONE → LAST_7_DAYS + sql_active_chats_by_date + sql_stats_by_date
def test_analytics_none_time_becomes_last_7_days_with_tools():
    qm = _qm(output_shape=QueryOutputShape.ANALYTICS, time_range=PlanTimeRange.NONE)
    plan = compile_query_model_to_plan(query_model=qm, query="analytics")
    assert plan.time_range == PlanTimeRange.LAST_7_DAYS
    names = _tool_names(plan)
    assert "sql_active_chats_by_date" in names
    assert "sql_stats_by_date" in names


# 7. LIST + no proof → SQL_DATE_SUMMARY, no rag_search
def test_list_no_proof_is_sql_date_summary_without_rag():
    qm = _qm(output_shape=QueryOutputShape.LIST, need_proof=False)
    plan = compile_query_model_to_plan(query_model=qm, query="list stuff")
    assert plan.strategy == PlanStrategy.SQL_DATE_SUMMARY
    assert "rag_search" not in _tool_names(plan)


# 8. LIST + proof → HYBRID + rag_search
def test_list_with_proof_is_hybrid_with_rag():
    qm = _qm(output_shape=QueryOutputShape.LIST, need_proof=True)
    plan = compile_query_model_to_plan(query_model=qm, query="list stuff")
    assert plan.strategy == PlanStrategy.HYBRID
    assert "rag_search" in _tool_names(plan)


# 9. LIST + chat_query, no chat_ids → sql_find_chats present
def test_list_chat_query_no_chat_ids_includes_find_chats():
    qm = _qm(output_shape=QueryOutputShape.LIST, need_proof=False, chat_query="Bob", chat_ids=None)
    plan = compile_query_model_to_plan(query_model=qm, query="list")
    assert "sql_find_chats" in _tool_names(plan)


# 10. LIST + chat_ids → sql_find_chats NOT present
def test_list_with_chat_ids_excludes_find_chats():
    qm = _qm(output_shape=QueryOutputShape.LIST, need_proof=False, chat_query="Bob", chat_ids=[123])
    plan = compile_query_model_to_plan(query_model=qm, query="list")
    assert "sql_find_chats" not in _tool_names(plan)


# 11. ANSWER + no proof → RAG_SEMANTIC + rag_search
def test_answer_no_proof_is_rag_semantic():
    qm = _qm(output_shape=QueryOutputShape.ANSWER, need_proof=False)
    plan = compile_query_model_to_plan(query_model=qm, query="what is x?")
    assert plan.strategy == PlanStrategy.RAG_SEMANTIC
    assert "rag_search" in _tool_names(plan)


# 12. ANSWER + proof → HYBRID
def test_answer_with_proof_is_hybrid():
    qm = _qm(output_shape=QueryOutputShape.ANSWER, need_proof=True)
    plan = compile_query_model_to_plan(query_model=qm, query="what is x?")
    assert plan.strategy == PlanStrategy.HYBRID


# 13. ANSWER + rag + chat_query → sql_find_chats present
def test_answer_rag_with_chat_query_includes_find_chats():
    qm = _qm(output_shape=QueryOutputShape.ANSWER, need_proof=False, chat_query="Alice", chat_ids=None)
    plan = compile_query_model_to_plan(query_model=qm, query="question")
    assert "sql_find_chats" in _tool_names(plan)


# 14. RECENT_MESSAGES → SQL_DATE_SUMMARY + sql_recent_messages_by_chat_query
def test_recent_messages_strategy_and_tool():
    qm = _qm(operation=QueryOperation.RECENT_MESSAGES, scope=PlanScope.CURRENT_CHAT)
    plan = compile_query_model_to_plan(query_model=qm, query="recent")
    assert plan.strategy == PlanStrategy.SQL_DATE_SUMMARY
    assert "sql_recent_messages_by_chat_query" in _tool_names(plan)


# 15. RECENT_MESSAGES → get_recent_dialog is first tool
def test_recent_messages_first_tool_is_get_recent_dialog():
    qm = _qm(operation=QueryOperation.RECENT_MESSAGES, scope=PlanScope.CURRENT_CHAT)
    plan = compile_query_model_to_plan(query_model=qm, query="recent")
    assert _tool_names(plan)[0] == "get_recent_dialog"


# 16. MEDIA_MESSAGES → sql_media_messages_by_chat_query
def test_media_messages_uses_media_tool():
    qm = _qm(operation=QueryOperation.MEDIA_MESSAGES, scope=PlanScope.CURRENT_CHAT, media_type="photo")
    plan = compile_query_model_to_plan(query_model=qm, query="photos")
    assert "sql_media_messages_by_chat_query" in _tool_names(plan)


# 17. CHAT_LIST → sql_chats_by_topic in tools
def test_chat_list_includes_chats_by_topic():
    qm = _qm(operation=QueryOperation.CHAT_LIST)
    plan = compile_query_model_to_plan(query_model=qm, query="chats about AI")
    assert "sql_chats_by_topic" in _tool_names(plan)


# 18. CHAT_LIST + 3 variants → 3 sql_chats_by_topic calls
def test_chat_list_three_variants_produces_three_topic_calls():
    qm = QueryModel(
        operation=QueryOperation.CHAT_LIST,
        query_variants=["AI", "машинное обучение", "нейросети"],
    )
    plan = compile_query_model_to_plan(query_model=qm, query="chats about AI")
    topic_calls = [n for n in _tool_names(plan) if n == "sql_chats_by_topic"]
    assert len(topic_calls) == 3


# 19. DYNAMIC_QUERY → sql_dynamic_query in tools
def test_dynamic_query_uses_dynamic_tool():
    spec = DynamicToolSpec(
        select=[DynamicSelect(field="chat_id", agg=DynamicSelectAgg.COUNT, as_name="cnt")],
        require_time_range=False,
    )
    qm = QueryModel(operation=QueryOperation.DYNAMIC_QUERY, dynamic_tool=spec)
    plan = compile_query_model_to_plan(query_model=qm, query="count chats")
    assert "sql_dynamic_query" in _tool_names(plan)


# 20. scope + chat_types propagate to plan
def test_scope_and_chat_types_propagate_to_plan():
    qm = _qm(
        output_shape=QueryOutputShape.SUMMARY,
        time_range=PlanTimeRange.YESTERDAY,
        scope=PlanScope.ALL_CHATS,
        chat_types=[PlanChatType.PRIVATE, PlanChatType.GROUP],
    )
    plan = compile_query_model_to_plan(query_model=qm, query="summary")
    assert plan.scope == PlanScope.ALL_CHATS
    assert plan.chat_types == [PlanChatType.PRIVATE, PlanChatType.GROUP]

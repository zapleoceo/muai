"""Few-shot examples for the query router LLM."""

QUERY_FEWSHOTS: list[tuple[str, dict]] = [
    (
        "Саммари за вчера?",
        {"output_shape": "SUMMARY", "operation": "SEARCH", "need_proof": False, "precision_bias": "BALANCED",
         "constraints": {"scope": "ALL_CHATS", "time_range": "YESTERDAY"},
         "query_variants": ["саммари за вчера", "итоги за вчера"], "subqueries": [], "clarify_question": None, "max_steps": 2, "on_empty": "RETRY", "notes": None},
    ),
    (
        "О чем с Евочкой говорили вчера?",
        {"output_shape": "SUMMARY", "operation": "SEARCH", "need_proof": False, "precision_bias": "BALANCED",
         "constraints": {"scope": "ALL_CHATS", "chat_types": ["private"], "chat_query": "Евочка", "time_range": "YESTERDAY"},
         "query_variants": ["о чём говорили", "итоги переписки"], "subqueries": [], "clarify_question": None, "max_steps": 2, "on_empty": "RETRY", "notes": None},
    ),
    (
        "В чате Евочка Моя какое последнее сообщение есть?",
        {"output_shape": "LIST", "operation": "RECENT_MESSAGES", "need_proof": True, "precision_bias": "PRECISION",
         "constraints": {"scope": "ALL_CHATS", "chat_types": ["private"], "chat_query": "Евочка Моя", "time_range": "NONE", "limit": 3},
         "query_variants": [], "subqueries": [], "clarify_question": None, "max_steps": 2, "on_empty": "RETRY", "notes": None},
    ),
    (
        "Покажи голосовые в чате Евочка Моя",
        {"output_shape": "LIST", "operation": "MEDIA_MESSAGES", "need_proof": True, "precision_bias": "PRECISION",
         "constraints": {"scope": "ALL_CHATS", "chat_types": ["private"], "chat_query": "Евочка Моя", "media_type": "voice", "time_range": "LAST_30_DAYS", "limit": 20},
         "query_variants": [], "subqueries": [], "clarify_question": None, "max_steps": 2, "on_empty": "RETRY", "notes": None},
    ),
    (
        "Найди афишу на эту неделю на веранде",
        {"output_shape": "ANSWER", "operation": "SEARCH", "need_proof": True, "precision_bias": "BALANCED",
         "constraints": {"scope": "ALL_CHATS", "time_range": "LAST_7_DAYS"},
         "query_variants": ["афиша веранда", "расписание веранда", "veranda schedule", "афиша veranda"],
         "subqueries": [], "clarify_question": None, "max_steps": 2, "on_empty": "RETRY", "notes": None},
    ),
    (
        "а есть чаты в базе за вчера?",
        {"output_shape": "ANALYTICS", "operation": "SEARCH", "need_proof": False, "precision_bias": "BALANCED",
         "constraints": {"scope": "ALL_CHATS", "time_range": "YESTERDAY", "limit": 50},
         "query_variants": [], "subqueries": [], "clarify_question": None, "max_steps": 2, "on_empty": "RETRY", "notes": None},
    ),
]

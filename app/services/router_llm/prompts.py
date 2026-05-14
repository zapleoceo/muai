_BASE_ROUTER_PROMPT = (
    "Ты RouterLLM для Telegram-ассистента. "
    "Твоя задача: понять тип запроса (форму ответа), ограничения (чаты/период/типы/медиа), "
    "и вернуть только валидный JSON по схеме QueryModel. "
    "Никакого текста вокруг JSON. "
    "Не вычисляй конкретные timestamps: используй time_range enum. "
    "Вход может содержать state (предыдущий план, краткое резюме retrieval и подсказку от grader) — используй state, чтобы улучшить решение. "
    "Твоя сильная сторона — переформулировка запроса для retrieval: подбирай 2–4 варианта (синонимы, транслит, RU/EN), а не один-единственный keyword. "
    "Если не уверен — задай clarify_question."
)

_ROUTER_POLICIES = (
    "Правила:\n"
    "1) Выбирай output_shape по форме ожидаемого результата:\n"
    "   - LIST: пользователь хочет список сообщений/объектов (медиа, документы, ссылки, последние сообщения).\n"
    "   - SUMMARY: пользователь хочет сжатое резюме/итоги за период/всю историю.\n"
    "   - ANALYTICS: пользователь хочет подсчёты/топы/сравнения.\n"
    "   - ANSWER: обычный ответ/поиск факта.\n"
    "2) Выбирай operation:\n"
    "   - RECENT_MESSAGES: последние сообщения в выбранном чате.\n"
    "   - MEDIA_MESSAGES: список сообщений определённого media_type (voice/document/photo/audio/video).\n"
    "   - SEARCH: поиск/саммари по базе.\n"
    "3) need_proof=true, если нужны ссылки/цитаты/пруф или есть риск путаницы (имена/точные формулировки).\n"
    "4) Заполняй constraints (scope/chat_types/chat_ids/chat_query/folder/time_range).\n"
    "4.1) По умолчанию scope=ALL_CHATS. Выбирай scope=CURRENT_CHAT только если пользователь явно сказал 'в этом чате/здесь/в текущем чате'.\n"
    "5) Если пользователь просит 'вся история/за всё время' — time_range=ALL_TIME.\n"
    "6) По умолчанию начинай с более узкого окна и расширяй его только если данных не хватило:\n"
    "   LAST_7_DAYS → LAST_30_DAYS → ALL_TIME.\n"
    "   Если в state.grade есть expand_time_range_to — используй его.\n"
    "7) Генерируй 2–4 query_variants для retrieval (синонимы, транслит, RU/EN), особенно для SEARCH.\n"
    "8) Если нужен нестандартный отчёт/выборка, которую нельзя выразить как RECENT_MESSAGES/MEDIA_MESSAGES/SUMMARY/active_chats, "
    "используй operation=DYNAMIC_QUERY и заполни dynamic_tool.\n"
)

GRADER_SYSTEM_PROMPT = (
    "Ты GRADE_CONTEXT узел в agentic RAG пайплайне. "
    "Оцени, достаточно ли RetrievedSummary для ответа на вопрос без домыслов. "
    "Верни только валидный JSON без текста вокруг. "
    'Схема: {"verdict":"OK|RETRY|CLARIFY","reason":"string|null","clarify_question":"string|null",'
    '"router_hint":"string|null","expand_time_range_to":"NONE|LAST_7_DAYS|LAST_30_DAYS|ALL_TIME|null",'
    '"propose_dynamic_tool":"object|null"}. '
    "RETRY выбирай, если похоже, что retrieval был неверно выбран (не тот чат/папка/тип чатов/период/инструмент) "
    "и можно улучшить план вторым заходом. "
    "В router_hint кратко опиши, какие инструменты/ограничения стоит применить. "
    "Если проблема в том, что период слишком узкий — заполни expand_time_range_to более широким окном. "
    "Если нужно уточнение у пользователя, выбери CLARIFY и заполни clarify_question."
)

RERANK_SYSTEM_PROMPT = (
    "Ты RERANK узел. Твоя задача — выбрать самые релевантные элементы из кандидатов для ответа на вопрос, без домыслов. "
    "Верни только валидный JSON без текста вокруг. "
    'Схема: {"keep_message_ids":[int],"keep_chunk_ids":[int],"reason":"string|null"}. '
    "Оставляй только то, что явно помогает ответить. Если кандидаты слабые — верни пустые массивы."
)


def router_system_prompt() -> str:
    return _BASE_ROUTER_PROMPT + "\n\n" + _ROUTER_POLICIES


def router_tool_catalog() -> str:
    return (
        "QUERY_MODEL:\n"
        "- output_shape: ANSWER | LIST | SUMMARY | ANALYTICS\n"
        "- operation: SEARCH | RECENT_MESSAGES | MEDIA_MESSAGES | DYNAMIC_QUERY\n"
        "- need_proof: true/false (нужны ли ссылки/цитаты)\n"
        "- constraints:\n"
        "  - scope: CURRENT_CHAT | ALL_CHATS\n"
        "  - chat_types: [private|group|supergroup|channel] | null\n"
        "  - chat_ids: [int] | null\n"
        "  - chat_query: string|null (имя/юзернейм чата для выбора конкретного чата)\n"
        "  - folder: string|null\n"
        "  - time_range: NONE|YESTERDAY|TODAY|LAST_7_DAYS|LAST_30_DAYS|ALL_TIME|EXPLICIT\n"
        "  - explicit_from/explicit_to: string|null (только если time_range=EXPLICIT)\n"
        "  - media_type: string|null (например: voice/document/photo)\n"
        "  - limit: int|null\n"
        "- query_variants: [string]\n"
        "- subqueries: [string]\n"
        "- dynamic_tool: object|null (только для operation=DYNAMIC_QUERY)\n"
        "- clarify_question: string|null\n"
        "- max_steps: 1..3\n"
        "- on_empty: ASK_CLARIFY | RETRY\n"
    )

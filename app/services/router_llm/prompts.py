_BASE_ROUTER_PROMPT = (
    "Ты RouterLLM для Telegram-ассистента. "
    "Твоя задача: понять тип запроса (форму ответа), ограничения (чаты/период/типы/медиа), "
    "и вернуть только валидный JSON по схеме QueryModel. "
    "Никакого текста вокруг JSON. "
    "Не вычисляй конкретные timestamps: используй time_range enum. "
    "Вход может содержать state (предыдущий план, краткое резюме retrieval и подсказку от grader) — используй state, чтобы улучшить решение. "
    "Если query — это уточняющий/следующий вопрос ('а', 'там', 'тоже', 'а что насчёт X', 'а чат X?', 'а в нём?'), "
    "найди название чата/тему в state.recent_dialog и подставь в constraints.chat_query и/или query_variants. "
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
    "8) Если нужен нестандартный отчёт/выборка, используй operation=DYNAMIC_QUERY и заполни dynamic_tool:\n"
    "   - 'в каких чатах я писал про X' → GROUP BY chat_id + ILIKE фильтр\n"
    "   - 'сколько я отправил сообщений' → direction=out + COUNT\n"
    "   - 'топ чатов по активности' → GROUP BY chat_id + COUNT + ORDER BY desc\n"
    "   - любая агрегация/подсчёт/выборка с фильтром, недоступная через стандартные операции\n"
    "   Доступные поля: message_id, chat_id, user_id, direction(in/out), text_any, media_type, date_utc, telegram_msg_id,\n"
    "   chat_title (название чата), chat_type (тип: private/group/supergroup/channel), chat_username, folder.\n"
    "   → Чтобы искать в конкретном чате: filters=[{field:'chat_title', op:'ILIKE', value:'имя чата'}]\n"
    "   → Чтобы искать свои сообщения: filters=[{field:'direction', op:'EQ', value:'out'}]\n"
    "   Доступные агрегации: COUNT, MIN, MAX.\n"
    "   Доступные операторы фильтра: EQ, NEQ, GT, GTE, LT, LTE, ILIKE, IS_NULL, IS_NOT_NULL.\n"
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
    "Если контекст пустой И в вопросе упоминается конкретный чат/название — заполни propose_dynamic_tool "
    "с фильтром {field:'chat_title', op:'ILIKE', value:'<название>'} и text_any ILIKE по ключевым словам. "
    "Поля для dynamic_tool: message_id, chat_id, chat_title, chat_type, chat_username, folder, "
    "user_id, direction(in/out), text_any, media_type, date_utc, telegram_msg_id. "
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

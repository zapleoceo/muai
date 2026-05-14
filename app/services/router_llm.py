import json
import re
from datetime import datetime

from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider
from app.services.answering_types import Plan


_BASE_ROUTER_PROMPT = (
    "Ты RouterLLM для Telegram-ассистента. "
    "Твоя задача: выбрать стратегию ответа и список инструментов, "
    "вернуть только валидный JSON по схеме Plan. "
    "Никакого текста вокруг JSON. "
    "Не вычисляй конкретные timestamps: используй time_range enum. "
    "Вход может содержать state (предыдущий план, краткое резюме retrieval и подсказку от grader) — используй state, чтобы улучшить план. "
    "Твоя сильная сторона — переформулировка запроса для retrieval: подбирай 2–4 варианта (синонимы, транслит, RU/EN), а не один-единственный keyword. "
    "Если не уверен — задай clarify_question и используй стратегию INFO_ONLY."
)

_ROUTER_POLICIES = (
    "Правила:\n"
    "1) Для большинства стратегий, кроме COMMAND, включай get_recent_dialog(limit=20) для CURRENT_CHAT.\n"
    "   Если в state есть recent_dialog — используй его как контекст и можешь НЕ дублировать get_recent_dialog.\n"
    "2) Если пользователь явно ограничивает тип чатов:\n"
    "   - только личные → chat_types=['private'], scope='ALL_CHATS'\n"
    "   - только группы → chat_types=['group','supergroup'], scope='ALL_CHATS'\n"
    "   - только каналы → chat_types=['channel'], scope='ALL_CHATS'\n"
    "3) Если пользователь спрашивает 'о чём' с конкретным человеком/чатом — по умолчанию предполагай личный чат:\n"
    "   chat_types=['private'], scope='ALL_CHATS'\n"
    "4) Если пользователь просит ссылку/пруф/исходник — используй инструменты, которые возвращают link (sql_lex_search_messages или sql_search_messages).\n"
    "5) Если в вопросе явно указан чат/человек и есть time_range — предпочитай sql_messages_by_chat_query_and_date, чтобы не тянуть лишнее.\n"
    "6) Если пользователь просит выборку/саммари по папке (folder) — используй sql_messages_by_folder_and_date.\n"
    "7) Если вопрос про поиск по базе (включая расплывчатые формулировки) — предпочитай sql_lex_search_messages (он сочетает FTS+trgm).\n"
    "   Если нужен период, включи use_time_range=true, но делай 2–4 tool-calls с разными query-variant.\n"
    "   Если лексика не даёт результата — добавь rag_search как второй канал.\n"
    "   - добавляй синонимы (например: анонс/афиша/расписание; кино/фильм/сеанс; встреча/ивент/event)\n"
    "   - добавляй RU/EN варианты и транслит (веранда/veranda)\n"
    "   - добавляй 'якоря' формата (например: '📍', '🕖', 'вход', диапазон дат '11.05-17.05')\n"
    "8) Если пользователь прислал ссылку t.me/.../MSG_ID — это точный идентификатор. Используй sql_message_by_tg_ref(chat_username или chat_id, telegram_msg_id) и верни найденное сообщение.\n"
    "9) Если в запросе упоминается чат/папка/место, но непонятно какой именно чат нужен — сначала используй sql_find_chats.\n"
    "   Если запрос может быть в RU, а чат назван в EN (или наоборот) — вызови sql_find_chats 2 раза с query-variant (например: 'веранда' и 'veranda').\n"
    "   Если после sql_find_chats есть кандидаты — во втором шаге ограничь chat_ids выбранными кандидатами.\n"
    "10) Для стратегий SQL_DATE_SUMMARY и HYBRID ставь max_steps=2 и on_empty='RETRY', чтобы можно было сделать второй заход retrieval.\n"
    "11) Не привязывайся к одному дню, если пользователь спрашивает про недельное расписание/афишу: пост часто публикуют накануне. Используй LAST_7_DAYS или более широкий EXPLICIT.\n"
    "12) Если пользователь просит 'последнее сообщение/крайний текст' в конкретном чате — используй sql_recent_messages_by_chat_query (limit 1..5) и не проси подтверждений.\n"
    "12.1) Если пользователь просит показать медиа определённого типа (например: голосовые/voice, фото/photo, документы/document) в конкретном чате — используй sql_media_messages_by_chat_query.\n"
    "13) Не используй стратегию COMMAND в этом проекте. Если нужны уточнения — используй INFO_ONLY + clarify_question.\n"
    "14) Если пользователь просит саммари/поиск по всей истории ('вся история', 'за всё время') — используй time_range=ALL_TIME.\n"
    "15) Для лучших ответов по умолчанию начинай с более узкого окна и расширяй его только если данных не хватило:\n"
    "   - LAST_7_DAYS → LAST_30_DAYS → ALL_TIME.\n"
    "   Решение о расширении бери из state.grade.expand_time_range_to, если оно есть.\n"
)


def _router_system_prompt() -> str:
    return _BASE_ROUTER_PROMPT + "\n\n" + _ROUTER_POLICIES


def _router_tool_catalog() -> str:
    return (
        "STRATEGIES:\n"
        "- INFO_ONLY: общий ответ + свежий контекст текущего чата.\n"
        "- RAG_SEMANTIC: семантический поиск по векторным чанкам.\n"
        "- SQL_DATE_SUMMARY: выборка сообщений по датам/периоду и последующее суммирование.\n"
        "- HYBRID: и SQL по датам, и RAG.\n"
        "- COMMAND: только явные команды, опасно.\n\n"
        "TOOLS:\n"
        "- get_recent_dialog(chat_id, limit)\n"
        "- rag_search(scope, query, top_k)  # ищет по векторным чанкам текста и файлов (message_chunks + media_chunks)\n"
        "- sql_find_chats(query, limit, chat_types?)\n"
        "- sql_lex_search_messages(scope, query, limit, chat_types?, chat_ids?, use_time_range?)\n"
        "- sql_search_messages(scope, query, limit, chat_types?, chat_ids?)\n"
        "- sql_search_messages_by_date(scope, time_range, query, limit, chat_types?, chat_ids?)\n"
        "- sql_message_by_tg_ref(chat_username?, chat_id?, telegram_msg_id)\n"
        "- sql_recent_messages_by_chat_query(scope, chat_query, limit, chat_types?)\n"
        "- sql_media_messages_by_chat_query(scope, chat_query?, media_type, limit, chat_types?, use_time_range?)\n"
        "- sql_messages_by_chat_query_and_date(scope, time_range, chat_query, max_rows, chat_types?)\n"
        "- sql_messages_by_folder_and_date(scope, time_range, folder, max_rows, chat_types?)\n"
        "- sql_messages_by_date(scope, time_range, explicit_from?, explicit_to?, max_rows, chat_types?, chat_ids?)\n"
        "- sql_stats_by_date(scope, time_range, explicit_from?, explicit_to?, chat_types?, chat_ids?)\n"
    )


_FEWSHOTS: list[tuple[str, dict]] = [
    (
        "Саммари за вчера?",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {"name": "sql_messages_by_date", "args": {"scope": "ALL_CHATS", "max_rows": 1500}},
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS"}},
            ],
            "time_range": "YESTERDAY",
            "scope": "ALL_CHATS",
            "chat_types": None,
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Саммари за последние 7 дней",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {"name": "sql_messages_by_date", "args": {"scope": "ALL_CHATS", "max_rows": 2000}},
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS"}},
            ],
            "time_range": "LAST_7_DAYS",
            "scope": "ALL_CHATS",
            "chat_types": None,
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Что было в мае?",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [{"name": "get_recent_dialog", "args": {"limit": 20}}],
            "time_range": "EXPLICIT",
            "scope": "ALL_CHATS",
            "chat_types": None,
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": "Уточни, пожалуйста: какой год и какие даты мая нужны (например, 2026-05-01..2026-05-31)?",
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "саммари за вчера по чатам из папки ItStep",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {"name": "sql_messages_by_folder_and_date", "args": {"scope": "ALL_CHATS", "max_rows": 1500, "folder": "ItStep"}},
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS"}},
            ],
            "time_range": "YESTERDAY",
            "scope": "ALL_CHATS",
            "chat_types": None,
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "какой фильм вчера показывали на веранде ?",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {
                    "name": "sql_lex_search_messages",
                    "args": {"scope": "ALL_CHATS", "limit": 60, "chat_types": ["group", "supergroup"], "query": "Veranda", "use_time_range": True},
                },
                {
                    "name": "sql_lex_search_messages",
                    "args": {"scope": "ALL_CHATS", "limit": 60, "chat_types": ["group", "supergroup"], "query": "веранда", "use_time_range": True},
                },
            ],
            "time_range": "YESTERDAY",
            "scope": "ALL_CHATS",
            "chat_types": ["group", "supergroup"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "анонс события на веранде на эту неделю ?",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {"name": "sql_find_chats", "args": {"limit": 8, "query": "Veranda", "chat_types": ["group", "supergroup", "channel"]}},
                {"name": "sql_find_chats", "args": {"limit": 8, "query": "веранда", "chat_types": ["group", "supergroup", "channel"]}},
                {"name": "sql_lex_search_messages", "args": {"scope": "ALL_CHATS", "limit": 140, "query": "афиша", "chat_types": ["group", "supergroup", "channel"], "use_time_range": True}},
                {"name": "sql_lex_search_messages", "args": {"scope": "ALL_CHATS", "limit": 140, "query": "анонс", "chat_types": ["group", "supergroup", "channel"], "use_time_range": True}},
                {"name": "sql_lex_search_messages", "args": {"scope": "ALL_CHATS", "limit": 140, "query": "📍", "chat_types": ["group", "supergroup", "channel"], "use_time_range": True}},
            ],
            "time_range": "LAST_7_DAYS",
            "scope": "ALL_CHATS",
            "chat_types": ["group", "supergroup", "channel"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "посмотри был где-то в группах анонс в понедельник со всеми событиями на веранде на неделю",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {"name": "sql_find_chats", "args": {"limit": 10, "query": "veranda", "chat_types": ["group", "supergroup", "channel"]}},
                {"name": "sql_find_chats", "args": {"limit": 10, "query": "веранда", "chat_types": ["group", "supergroup", "channel"]}},
                {"name": "sql_lex_search_messages", "args": {"scope": "ALL_CHATS", "limit": 180, "query": "афиша", "chat_types": ["group", "supergroup", "channel"], "use_time_range": True}},
                {"name": "sql_lex_search_messages", "args": {"scope": "ALL_CHATS", "limit": 180, "query": "распис", "chat_types": ["group", "supergroup", "channel"], "use_time_range": True}},
                {"name": "sql_lex_search_messages", "args": {"scope": "ALL_CHATS", "limit": 180, "query": "11.05-17.05", "chat_types": ["group", "supergroup", "channel"], "use_time_range": True}},
            ],
            "time_range": "LAST_7_DAYS",
            "scope": "ALL_CHATS",
            "chat_types": ["group", "supergroup", "channel"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Сводка за вчера только по личным чатам, без групп и каналов",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {
                    "name": "sql_messages_by_date",
                    "args": {"scope": "ALL_CHATS", "max_rows": 1500, "chat_types": ["private"]},
                },
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS", "chat_types": ["private"]}},
            ],
            "time_range": "YESTERDAY",
            "scope": "ALL_CHATS",
            "chat_types": ["private"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "О чем с Евочкой говорили вчера?",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {
                    "name": "sql_messages_by_chat_query_and_date",
                    "args": {"scope": "ALL_CHATS", "max_rows": 1500, "chat_types": ["private"], "chat_query": "Евочка"},
                },
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS", "chat_types": ["private"]}},
            ],
            "time_range": "YESTERDAY",
            "scope": "ALL_CHATS",
            "chat_types": ["private"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "В чате Евочка Моя какое последнее сообщение есть ?",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {"name": "sql_recent_messages_by_chat_query", "args": {"scope": "ALL_CHATS", "chat_types": ["private"], "chat_query": "Евочка Моя", "limit": 3}},
            ],
            "time_range": "NONE",
            "scope": "ALL_CHATS",
            "chat_types": ["private"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Саммари по чати с Евочкой ?",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {
                    "name": "sql_messages_by_chat_query_and_date",
                    "args": {"scope": "ALL_CHATS", "max_rows": 2000, "chat_types": ["private"], "chat_query": "Евочка"},
                },
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS", "chat_types": ["private"]}},
            ],
            "time_range": "LAST_7_DAYS",
            "scope": "ALL_CHATS",
            "chat_types": ["private"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Сделай саммари по всей истории чата с Евочкой",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {
                    "name": "sql_messages_by_chat_query_and_date",
                    "args": {"scope": "ALL_CHATS", "max_rows": 2500, "chat_types": ["private"], "chat_query": "Евочка"},
                },
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS", "chat_types": ["private"]}},
            ],
            "time_range": "ALL_TIME",
            "scope": "ALL_CHATS",
            "chat_types": ["private"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Проанализируй всю историю чата с евочкой",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {
                    "name": "sql_messages_by_chat_query_and_date",
                    "args": {"scope": "ALL_CHATS", "max_rows": 2500, "chat_types": ["private"], "chat_query": "Евочка"},
                },
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS", "chat_types": ["private"]}},
            ],
            "time_range": "ALL_TIME",
            "scope": "ALL_CHATS",
            "chat_types": ["private"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Дай ссылку на это сообщение",
        {
            "strategy": "HYBRID",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {"name": "sql_search_messages", "args": {"scope": "ALL_CHATS", "limit": 20}},
                {"name": "rag_search", "args": {"scope": "ALL_CHATS", "top_k": 6}},
            ],
            "time_range": "NONE",
            "scope": "ALL_CHATS",
            "chat_types": None,
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": "Уточни, про какое именно сообщение речь: из какого чата/примерное время/цитата. Пока покажу ближайшие совпадения и ссылки, если они доступны.",
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Сводка за вчера только по группам",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {
                    "name": "sql_messages_by_date",
                    "args": {"scope": "ALL_CHATS", "max_rows": 1500, "chat_types": ["group", "supergroup"]},
                },
                {"name": "sql_stats_by_date", "args": {"scope": "ALL_CHATS", "chat_types": ["group", "supergroup"]}},
            ],
            "time_range": "YESTERDAY",
            "scope": "ALL_CHATS",
            "chat_types": ["group", "supergroup"],
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
        },
    ),
    (
        "Что я писал про маркетинг в Джакарте?",
        {
            "strategy": "RAG_SEMANTIC",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {"name": "rag_search", "args": {"scope": "ALL_CHATS", "top_k": 8}},
            ],
            "time_range": "NONE",
            "scope": "ALL_CHATS",
            "chat_types": None,
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 1,
            "on_empty": "ASK_CLARIFY",
        },
    ),
    (
        "Объясни простыми словами, что такое pgvector",
        {
            "strategy": "INFO_ONLY",
            "tools": [{"name": "get_recent_dialog", "args": {"limit": 20}}],
            "time_range": "NONE",
            "scope": "CURRENT_CHAT",
            "chat_types": None,
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": None,
            "max_steps": 1,
            "on_empty": "ASK_CLARIFY",
        },
    ),
    (
        "Удалить все сообщения за вчера",
        {
            "strategy": "COMMAND",
            "tools": [],
            "time_range": "YESTERDAY",
            "scope": "CURRENT_CHAT",
            "chat_types": None,
            "chat_ids": None,
            "explicit_from": None,
            "explicit_to": None,
            "clarify_question": "Подтверди команду и уточни scope: только этот чат или все чаты?",
            "max_steps": 1,
            "on_empty": "ASK_CLARIFY",
        },
    ),
]


def _extract_json(text: str) -> dict:
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        return json.loads(s)
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        raise ValueError("No JSON object found")
    return json.loads(m.group(0))


def _extract_tg_link_ref(query: str) -> dict | None:
    s = str(query or "")

    m = re.search(r"https?://t\.me/c/(?P<internal>\d+)(?:/\d+)?/(?P<msg>\d+)", s)
    if m:
        internal = m.group("internal")
        msg = int(m.group("msg"))
        return {"chat_id": int(f"-100{internal}"), "telegram_msg_id": msg}

    m = re.search(r"https?://t\.me/(?P<username>[A-Za-z0-9_]{3,})(?:/\d+)?/(?P<msg>\d+)", s)
    if m:
        username = m.group("username")
        msg = int(m.group("msg"))
        return {"chat_username": username, "telegram_msg_id": msg}

    return None


def _build_plan_for_tg_ref(ref: dict) -> dict:
    args = {"telegram_msg_id": int(ref["telegram_msg_id"])}
    if ref.get("chat_id") is not None:
        args["chat_id"] = int(ref["chat_id"])
    if ref.get("chat_username") is not None:
        args["chat_username"] = str(ref["chat_username"])

    return {
        "strategy": "SQL_DATE_SUMMARY",
        "tools": [
            {"name": "get_recent_dialog", "args": {"limit": 20}},
            {"name": "sql_message_by_tg_ref", "args": args},
        ],
        "time_range": "NONE",
        "scope": "ALL_CHATS",
        "chat_types": None,
        "chat_ids": None,
        "explicit_from": None,
        "explicit_to": None,
        "clarify_question": None,
        "max_steps": 2,
        "on_empty": "RETRY",
        "notes": "tg_link_ref",
    }


_GRADER_SYSTEM_PROMPT = (
    "Ты GRADE_CONTEXT узел в agentic RAG пайплайне. "
    "Оцени, достаточно ли RetrievedSummary для ответа на вопрос без домыслов. "
    "Верни только валидный JSON без текста вокруг. "
    "Схема: {"
    '"verdict":"OK|RETRY|CLARIFY",'
    '"reason":"string|null",'
    '"clarify_question":"string|null",'
    '"router_hint":"string|null",'
    '"expand_time_range_to":"NONE|LAST_7_DAYS|LAST_30_DAYS|ALL_TIME|null"'
    "}. "
    "RETRY выбирай, если похоже, что retrieval был неверно выбран (не тот чат/папка/тип чатов/период/инструмент) "
    "и можно улучшить план вторым заходом. "
    "В router_hint кратко опиши, какие инструменты/ограничения стоит применить (sql_find_chats, sql_messages_by_date, sql_lex_search_messages, sql_search_messages_by_date, rag_search). "
    "Если проблема в том, что период слишком узкий — заполни expand_time_range_to более широким окном (LAST_30_DAYS или ALL_TIME). "
    "Если нужно уточнение у пользователя, выбери CLARIFY и заполни clarify_question."
)


_RERANK_SYSTEM_PROMPT = (
    "Ты RERANK узел. Твоя задача — выбрать самые релевантные элементы из кандидатов для ответа на вопрос, без домыслов. "
    "Верни только валидный JSON без текста вокруг. "
    "Схема: {"
    '"keep_message_ids":[int],'
    '"keep_chunk_ids":[int],'
    '"reason":"string|null"'
    "}. "
    "Оставляй только то, что явно помогает ответить. Если кандидаты слабые — верни пустые массивы."
)


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
    msgs = []
    for m in candidate_messages[:80]:
        msgs.append(
            {
                "message_id": m.get("message_id"),
                "chat_id": m.get("chat_id"),
                "chat_title": (m.get("chat") or {}).get("title"),
                "date_utc": m.get("date_utc"),
                "text": str(m.get("text") or "")[:800],
                "link": m.get("link"),
                "score": m.get("score"),
            }
        )
    chs = []
    for c in candidate_chunks[:60]:
        chs.append(
            {
                "chunk_id": c.get("chunk_id"),
                "chat_id": c.get("chat_id"),
                "chat_title": c.get("chat_title"),
                "msg_date_from": c.get("msg_date_from"),
                "msg_date_to": c.get("msg_date_to"),
                "text": str(c.get("text") or "")[:1000],
                "link": c.get("link"),
            }
        )
    input_block = {
        "query": query,
        "limits": {"keep_messages": int(keep_messages), "keep_chunks": int(keep_chunks)},
        "messages": msgs,
        "chunks": chs,
        "language": language,
    }
    messages = [LLMMessage(role="user", content=json.dumps(input_block, ensure_ascii=False))]
    raw = await provider.complete(messages, system_prompt=_RERANK_SYSTEM_PROMPT)
    return _extract_json(raw), raw


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
        "catalog": _router_tool_catalog(),
        "language": language,
    }
    messages = [LLMMessage(role="user", content=json.dumps(input_block, ensure_ascii=False))]
    raw = await provider.complete(messages, system_prompt=_GRADER_SYSTEM_PROMPT)
    return _extract_json(raw), raw

def _validate_plan_invariants(plan: Plan) -> None:
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
        if plan.max_steps != 1:
            raise ValueError("RAG_SEMANTIC: max_steps должен быть 1")
        if plan.on_empty.value != "ASK_CLARIFY":
            raise ValueError("RAG_SEMANTIC: on_empty должен быть 'ASK_CLARIFY'")

    if plan.strategy.value == "SQL_DATE_SUMMARY":
        if not any(
            n in tool_names
            for n in (
                "sql_messages_by_date",
                "sql_stats_by_date",
                "sql_search_messages_by_date",
                "sql_lex_search_messages",
                "sql_message_by_tg_ref",
                "sql_recent_messages_by_chat_query",
                "sql_media_messages_by_chat_query",
                "sql_messages_by_chat_query_and_date",
                "sql_messages_by_folder_and_date",
            )
        ):
            raise ValueError("SQL_DATE_SUMMARY требует SQL tool (messages/stats)")
        if plan.max_steps < 2:
            raise ValueError("SQL_DATE_SUMMARY: max_steps должен быть >= 2")
        if plan.on_empty.value != "RETRY":
            raise ValueError("SQL_DATE_SUMMARY: on_empty должен быть 'RETRY'")

    if plan.strategy.value == "HYBRID":
        if "rag_search" not in tool_names:
            raise ValueError("HYBRID требует rag_search")
        if not any(
            n in tool_names
            for n in (
                "sql_messages_by_date",
                "sql_stats_by_date",
                "sql_search_messages_by_date",
                "sql_lex_search_messages",
                "sql_message_by_tg_ref",
                "sql_recent_messages_by_chat_query",
                "sql_media_messages_by_chat_query",
                "sql_messages_by_chat_query_and_date",
                "sql_messages_by_folder_and_date",
            )
        ):
            raise ValueError("HYBRID требует SQL tool (messages/stats)")
        if plan.max_steps < 2:
            raise ValueError("HYBRID: max_steps должен быть >= 2")
        if plan.on_empty.value != "RETRY":
            raise ValueError("HYBRID: on_empty должен быть 'RETRY'")

    if plan.strategy.value == "COMMAND":
        if plan.tools:
            raise ValueError("COMMAND: инструменты должны быть пустыми (в этом проекте)")
        if plan.max_steps != 1:
            raise ValueError("COMMAND: max_steps должен быть 1")
        if plan.on_empty.value != "ASK_CLARIFY":
            raise ValueError("COMMAND: on_empty должен быть 'ASK_CLARIFY'")


async def route_query(
    *,
    query: str,
    user_id: int | None,
    chat_id: int,
    language: str = "ru",
    timezone: str = "UTC",
    state: dict | None = None,
) -> tuple[Plan, str]:
    def _time_range_rank(v: str) -> int:
        m = {"NONE": 0, "YESTERDAY": 1, "TODAY": 1, "LAST_7_DAYS": 2, "LAST_30_DAYS": 3, "ALL_TIME": 4, "EXPLICIT": 4}
        return int(m.get(v, 0))

    forced_time_range = None
    if state:
        f = state.get("force_time_range")
        if isinstance(f, str) and f:
            forced_time_range = f
    tg_ref = _extract_tg_link_ref(query)
    if tg_ref:
        plan_dict = _build_plan_for_tg_ref(tg_ref)
        plan = Plan.model_validate(plan_dict)
        _validate_plan_invariants(plan)
        return plan, json.dumps(plan_dict, ensure_ascii=False)

    provider = get_llm_provider()
    now = datetime.now().isoformat(timespec="seconds")

    input_block = {
        "query": query,
        "metadata": {
            "user_id": user_id,
            "chat_id": chat_id,
            "language": language,
            "timezone": timezone,
            "now": now,
        },
        "state": state,
        "catalog": _router_tool_catalog(),
        "schema_hint": {
            "strategy": "INFO_ONLY|RAG_SEMANTIC|SQL_DATE_SUMMARY|HYBRID|COMMAND",
            "tools": [{"name": "tool_name", "args": {"k": "v"}}],
            "time_range": "NONE|YESTERDAY|TODAY|LAST_7_DAYS|LAST_30_DAYS|ALL_TIME|EXPLICIT",
            "scope": "CURRENT_CHAT|ALL_CHATS",
            "chat_types": ["private|group|supergroup|channel"],
            "chat_ids": [123],
            "explicit_from": "ISO date/datetime | null",
            "explicit_to": "ISO date/datetime | null",
            "clarify_question": "string | null",
            "max_steps": "1..3",
            "on_empty": "ASK_CLARIFY|RETRY",
        },
        "few_shots": [{"q": q, "plan": p} for (q, p) in _FEWSHOTS],
    }

    messages = [LLMMessage(role="user", content=json.dumps(input_block, ensure_ascii=False))]

    raw = await provider.complete(messages, system_prompt=_router_system_prompt())
    try:
        plan = Plan.model_validate(_extract_json(raw))
        _validate_plan_invariants(plan)
        if forced_time_range and plan.strategy.value not in ("INFO_ONLY", "COMMAND"):
            if _time_range_rank(forced_time_range) > _time_range_rank(plan.time_range.value):
                plan = plan.model_copy(update={"time_range": forced_time_range, "explicit_from": None, "explicit_to": None})
                _validate_plan_invariants(plan)
        return plan, raw
    except Exception as exc:
        repair_prompt = (
            "Исправь вывод: верни только валидный JSON объекта Plan, без текста. "
            f"Ошибка валидации: {str(exc)[:300]}"
        )
        raw2 = await provider.complete(
            [LLMMessage(role="user", content=raw), LLMMessage(role="user", content=repair_prompt)],
            system_prompt=_router_system_prompt(),
        )
        try:
            plan2 = Plan.model_validate(_extract_json(raw2))
            _validate_plan_invariants(plan2)
            if forced_time_range and plan2.strategy.value not in ("INFO_ONLY", "COMMAND"):
                if _time_range_rank(forced_time_range) > _time_range_rank(plan2.time_range.value):
                    plan2 = plan2.model_copy(update={"time_range": forced_time_range, "explicit_from": None, "explicit_to": None})
                    _validate_plan_invariants(plan2)
            return plan2, raw2
        except Exception as exc2:
            fallback = {
                "strategy": "INFO_ONLY",
                "tools": [{"name": "get_recent_dialog", "args": {"limit": 20}}],
                "time_range": "NONE",
                "scope": "CURRENT_CHAT",
                "chat_types": None,
                "chat_ids": None,
                "explicit_from": None,
                "explicit_to": None,
                "clarify_question": (
                    "Не смог корректно разобрать запрос для поиска по базе. "
                    "Уточни, пожалуйста: какой чат/период/что именно нужно найти."
                ),
                "max_steps": 1,
                "on_empty": "ASK_CLARIFY",
                "notes": f"router_fallback:{str(exc2)[:120]}",
            }
            plan3 = Plan.model_validate(fallback)
            _validate_plan_invariants(plan3)
            return plan3, json.dumps(fallback, ensure_ascii=False)

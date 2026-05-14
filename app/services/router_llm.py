import json
import re
from datetime import datetime

from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider
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
    "5) Если пользователь просит 'вся история/за всё время' — time_range=ALL_TIME.\n"
    "6) По умолчанию начинай с более узкого окна и расширяй его только если данных не хватило:\n"
    "   LAST_7_DAYS → LAST_30_DAYS → ALL_TIME.\n"
    "   Если в state.grade есть expand_time_range_to — используй его.\n"
    "7) Генерируй 2–4 query_variants для retrieval (синонимы, транслит, RU/EN), особенно для SEARCH.\n"
    "8) Если нужен нестандартный отчёт/выборка, которую нельзя выразить как RECENT_MESSAGES/MEDIA_MESSAGES/SUMMARY/active_chats, используй operation=DYNAMIC_QUERY и заполни dynamic_tool.\n"
)


def _router_system_prompt() -> str:
    return _BASE_ROUTER_PROMPT + "\n\n" + _ROUTER_POLICIES


def _router_tool_catalog() -> str:
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
        "Покажи голосовые в чате Евочка Моя",
        {
            "strategy": "SQL_DATE_SUMMARY",
            "tools": [
                {"name": "get_recent_dialog", "args": {"limit": 20}},
                {
                    "name": "sql_media_messages_by_chat_query",
                    "args": {"scope": "ALL_CHATS", "chat_types": ["private"], "chat_query": "Евочка Моя", "media_type": "voice", "limit": 20, "use_time_range": True},
                },
            ],
            "time_range": "LAST_30_DAYS",
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

_QUERY_FEWSHOTS: list[tuple[str, dict]] = [
    (
        "Саммари за вчера?",
        {
            "output_shape": "SUMMARY",
            "operation": "SEARCH",
            "need_proof": False,
            "precision_bias": "BALANCED",
            "constraints": {"scope": "ALL_CHATS", "time_range": "YESTERDAY"},
            "query_variants": ["саммари за вчера", "итоги за вчера"],
            "subqueries": [],
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
            "notes": None,
        },
    ),
    (
        "О чем с Евочкой говорили вчера?",
        {
            "output_shape": "SUMMARY",
            "operation": "SEARCH",
            "need_proof": False,
            "precision_bias": "BALANCED",
            "constraints": {"scope": "ALL_CHATS", "chat_types": ["private"], "chat_query": "Евочка", "time_range": "YESTERDAY"},
            "query_variants": ["о чём говорили", "итоги переписки"],
            "subqueries": [],
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
            "notes": None,
        },
    ),
    (
        "В чате Евочка Моя какое последнее сообщение есть?",
        {
            "output_shape": "LIST",
            "operation": "RECENT_MESSAGES",
            "need_proof": True,
            "precision_bias": "PRECISION",
            "constraints": {"scope": "ALL_CHATS", "chat_types": ["private"], "chat_query": "Евочка Моя", "time_range": "NONE", "limit": 3},
            "query_variants": [],
            "subqueries": [],
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
            "notes": None,
        },
    ),
    (
        "Покажи голосовые в чате Евочка Моя",
        {
            "output_shape": "LIST",
            "operation": "MEDIA_MESSAGES",
            "need_proof": True,
            "precision_bias": "PRECISION",
            "constraints": {"scope": "ALL_CHATS", "chat_types": ["private"], "chat_query": "Евочка Моя", "media_type": "voice", "time_range": "LAST_30_DAYS", "limit": 20},
            "query_variants": [],
            "subqueries": [],
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
            "notes": None,
        },
    ),
    (
        "Найди афишу на эту неделю на веранде",
        {
            "output_shape": "ANSWER",
            "operation": "SEARCH",
            "need_proof": True,
            "precision_bias": "BALANCED",
            "constraints": {"scope": "ALL_CHATS", "time_range": "LAST_7_DAYS"},
            "query_variants": ["афиша веранда", "расписание веранда", "veranda schedule", "афиша veranda"],
            "subqueries": [],
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
            "notes": None,
        },
    ),
    (
        "а есть чаты в базе за вчера?",
        {
            "output_shape": "ANALYTICS",
            "operation": "SEARCH",
            "need_proof": False,
            "precision_bias": "BALANCED",
            "constraints": {"scope": "ALL_CHATS", "time_range": "YESTERDAY", "limit": 50},
            "query_variants": [],
            "subqueries": [],
            "clarify_question": None,
            "max_steps": 2,
            "on_empty": "RETRY",
            "notes": None,
        },
    ),
]

_FEWSHOTS = _QUERY_FEWSHOTS


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
                "sql_dynamic_query",
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
                "sql_dynamic_query",
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


def _compile_query_model_to_plan(*, query_model: QueryModel, query: str) -> Plan:
    c = query_model.constraints

    if query_model.clarify_question:
        plan = Plan(
            strategy=PlanStrategy.INFO_ONLY,
            tools=[PlanToolCall(name="get_recent_dialog", args={"limit": 20})],
            time_range=PlanTimeRange.NONE,
            scope=PlanScope.CURRENT_CHAT,
            chat_types=None,
            chat_ids=None,
            explicit_from=None,
            explicit_to=None,
            clarify_question=query_model.clarify_question,
            max_steps=1,
            on_empty=PlanOnEmpty.ASK_CLARIFY,
            notes="compiled:clarify",
        )
        _validate_plan_invariants(plan)
        return plan

    chat_types = c.chat_types
    if chat_types:
        chat_types = [PlanChatType(x) for x in chat_types]

    base_tools: list[PlanToolCall] = [PlanToolCall(name="get_recent_dialog", args={"limit": 20})]

    time_range = c.time_range
    explicit_from = c.explicit_from
    explicit_to = c.explicit_to

    if query_model.operation == QueryOperation.RECENT_MESSAGES:
        lim = int(c.limit or 5)
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools
            + [
                PlanToolCall(
                    name="sql_recent_messages_by_chat_query",
                    args={
                        "scope": c.scope.value,
                        "chat_query": str(c.chat_query or ""),
                        "limit": lim,
                        "chat_types": [ct.value for ct in (chat_types or [])] or None,
                    },
                )
            ],
            time_range=PlanTimeRange.NONE,
            scope=c.scope,
            chat_types=chat_types,
            chat_ids=c.chat_ids,
            explicit_from=None,
            explicit_to=None,
            clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)),
            on_empty=PlanOnEmpty.RETRY,
            notes="compiled:recent_messages",
        )
        _validate_plan_invariants(plan)
        return plan

    if query_model.operation == QueryOperation.MEDIA_MESSAGES:
        lim = int(c.limit or 30)
        use_time_range = bool(time_range.value != "NONE")
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools
            + [
                PlanToolCall(
                    name="sql_media_messages_by_chat_query",
                    args={
                        "scope": c.scope.value,
                        "chat_query": c.chat_query,
                        "media_type": str(c.media_type or ""),
                        "limit": lim,
                        "chat_types": [ct.value for ct in (chat_types or [])] or None,
                        "use_time_range": use_time_range,
                    },
                )
            ],
            time_range=time_range,
            scope=c.scope,
            chat_types=chat_types,
            chat_ids=c.chat_ids,
            explicit_from=explicit_from,
            explicit_to=explicit_to,
            clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)),
            on_empty=PlanOnEmpty.RETRY,
            notes="compiled:media_messages",
        )
        _validate_plan_invariants(plan)
        return plan

    if query_model.operation == QueryOperation.DYNAMIC_QUERY:
        if query_model.dynamic_tool is None:
            raise ValueError("dynamic_tool required")
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools
            + [
                PlanToolCall(
                    name="sql_dynamic_query",
                    args={
                        "scope": c.scope.value,
                        "chat_types": [ct.value for ct in (chat_types or [])] or None,
                        "chat_ids": c.chat_ids,
                        "spec": query_model.dynamic_tool.model_dump(),
                    },
                )
            ],
            time_range=time_range,
            scope=c.scope,
            chat_types=chat_types,
            chat_ids=c.chat_ids,
            explicit_from=explicit_from,
            explicit_to=explicit_to,
            clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)),
            on_empty=PlanOnEmpty.RETRY,
            notes="compiled:dynamic_query",
        )
        _validate_plan_invariants(plan)
        return plan

    if query_model.output_shape == QueryOutputShape.SUMMARY:
        tr = time_range
        if tr.value == "NONE":
            tr = PlanTimeRange.LAST_7_DAYS
            explicit_from = None
            explicit_to = None

        if c.folder:
            main_tool = PlanToolCall(
                name="sql_messages_by_folder_and_date",
                args={
                    "scope": c.scope.value,
                    "max_rows": 2000,
                    "folder": str(c.folder),
                    "chat_types": [ct.value for ct in (chat_types or [])] or None,
                },
            )
        elif c.chat_query:
            main_tool = PlanToolCall(
                name="sql_messages_by_chat_query_and_date",
                args={
                    "scope": c.scope.value,
                    "max_rows": 2000,
                    "chat_query": str(c.chat_query),
                    "chat_types": [ct.value for ct in (chat_types or [])] or None,
                },
            )
        else:
            main_tool = PlanToolCall(
                name="sql_messages_by_date",
                args={
                    "scope": c.scope.value,
                    "max_rows": 2000,
                    "chat_types": [ct.value for ct in (chat_types or [])] or None,
                    "chat_ids": c.chat_ids,
                },
            )

        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools
            + [
                main_tool,
                PlanToolCall(
                    name="sql_stats_by_date",
                    args={"scope": c.scope.value, "chat_types": [ct.value for ct in (chat_types or [])] or None, "chat_ids": c.chat_ids},
                ),
            ],
            time_range=tr,
            scope=c.scope,
            chat_types=chat_types,
            chat_ids=c.chat_ids,
            explicit_from=explicit_from,
            explicit_to=explicit_to,
            clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)),
            on_empty=PlanOnEmpty.RETRY,
            notes="compiled:summary",
        )
        _validate_plan_invariants(plan)
        return plan

    if query_model.output_shape == QueryOutputShape.LIST:
        lim = int(c.limit or 30)
        q = (query_model.query_variants[0] if query_model.query_variants else query) or query
        tools = list(base_tools)
        if time_range.value != "NONE":
            tools.append(
                PlanToolCall(
                    name="sql_search_messages_by_date",
                    args={
                        "scope": c.scope.value,
                        "query": q,
                        "limit": lim,
                        "chat_types": [ct.value for ct in (chat_types or [])] or None,
                        "chat_ids": c.chat_ids,
                    },
                )
            )
        else:
            tools.append(
                PlanToolCall(
                    name="sql_search_messages",
                    args={
                        "scope": c.scope.value,
                        "query": q,
                        "limit": lim,
                        "chat_types": [ct.value for ct in (chat_types or [])] or None,
                        "chat_ids": c.chat_ids,
                    },
                )
            )

        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=tools,
            time_range=time_range,
            scope=c.scope,
            chat_types=chat_types,
            chat_ids=c.chat_ids,
            explicit_from=explicit_from,
            explicit_to=explicit_to,
            clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)),
            on_empty=PlanOnEmpty.RETRY,
            notes="compiled:list",
        )
        _validate_plan_invariants(plan)
        return plan

    if query_model.output_shape == QueryOutputShape.ANALYTICS:
        tr = time_range
        if tr.value == "NONE":
            tr = PlanTimeRange.LAST_7_DAYS
            explicit_from = None
            explicit_to = None
        plan = Plan(
            strategy=PlanStrategy.SQL_DATE_SUMMARY,
            tools=base_tools
            + [
                PlanToolCall(
                    name="sql_active_chats_by_date",
                    args={
                        "scope": c.scope.value,
                        "limit": int(c.limit or 50),
                        "chat_types": [ct.value for ct in (chat_types or [])] or None,
                        "chat_ids": c.chat_ids,
                    },
                ),
                PlanToolCall(
                    name="sql_stats_by_date",
                    args={"scope": c.scope.value, "chat_types": [ct.value for ct in (chat_types or [])] or None, "chat_ids": c.chat_ids},
                ),
            ],
            time_range=tr,
            scope=c.scope,
            chat_types=chat_types,
            chat_ids=c.chat_ids,
            explicit_from=explicit_from,
            explicit_to=explicit_to,
            clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)),
            on_empty=PlanOnEmpty.RETRY,
            notes="compiled:analytics",
        )
        _validate_plan_invariants(plan)
        return plan

    need_proof = bool(query_model.need_proof)
    tr = time_range
    if need_proof:
        tools: list[PlanToolCall] = list(base_tools)
        variants = [v for v in (query_model.query_variants or []) if str(v).strip()]
        if not variants:
            variants = [str(query).strip()]
        variants = variants[:3]
        use_time_range = bool(tr.value != "NONE")
        for v in variants:
            tools.append(
                PlanToolCall(
                    name="sql_lex_search_messages",
                    args={
                        "scope": c.scope.value,
                        "query": v,
                        "limit": 60,
                        "chat_types": [ct.value for ct in (chat_types or [])] or None,
                        "chat_ids": c.chat_ids,
                        "use_time_range": use_time_range,
                    },
                )
            )
        tools.append(PlanToolCall(name="rag_search", args={"scope": c.scope.value, "query": str(query), "top_k": 10, "chat_ids": c.chat_ids}))
        plan = Plan(
            strategy=PlanStrategy.HYBRID,
            tools=tools,
            time_range=tr,
            scope=c.scope,
            chat_types=chat_types,
            chat_ids=c.chat_ids,
            explicit_from=explicit_from,
            explicit_to=explicit_to,
            clarify_question=None,
            max_steps=max(2, int(query_model.max_steps or 2)),
            on_empty=PlanOnEmpty.RETRY,
            notes="compiled:hybrid",
        )
        _validate_plan_invariants(plan)
        return plan

    plan = Plan(
        strategy=PlanStrategy.RAG_SEMANTIC,
        tools=base_tools + [PlanToolCall(name="rag_search", args={"scope": c.scope.value, "query": str(query), "top_k": 12, "chat_ids": c.chat_ids})],
        time_range=tr,
        scope=c.scope,
        chat_types=chat_types,
        chat_ids=c.chat_ids,
        explicit_from=explicit_from,
        explicit_to=explicit_to,
        clarify_question=None,
        max_steps=1,
        on_empty=PlanOnEmpty.ASK_CLARIFY,
        notes="compiled:rag",
    )
    _validate_plan_invariants(plan)
    return plan


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
            "output_shape": "ANSWER|LIST|SUMMARY|ANALYTICS",
            "operation": "SEARCH|RECENT_MESSAGES|MEDIA_MESSAGES|DYNAMIC_QUERY",
            "need_proof": "true|false",
            "constraints": {
                "scope": "CURRENT_CHAT|ALL_CHATS",
                "chat_types": ["private|group|supergroup|channel"],
                "chat_ids": [123],
                "chat_query": "string|null",
                "folder": "string|null",
                "time_range": "NONE|YESTERDAY|TODAY|LAST_7_DAYS|LAST_30_DAYS|ALL_TIME|EXPLICIT",
                "explicit_from": "ISO date/datetime | null",
                "explicit_to": "ISO date/datetime | null",
                "media_type": "string|null",
                "limit": "int|null",
            },
            "query_variants": ["string"],
            "subqueries": ["string"],
            "dynamic_tool": {
                "select": [{"field": "chat_id|chat_type|chat_title|date_utc|text_any|media_type", "as_name": "optional", "agg": "COUNT|COUNT_DISTINCT|MAX|MIN|null"}],
                "filters": [{"field": "chat_id|chat_type|chat_title|date_utc|text_any|media_type", "op": "EQ|ILIKE|IN|BETWEEN|IS_NOT_NULL", "value": "any", "value_to": "any|null"}],
                "group_by": ["field"],
                "order_by": [{"field": "field", "desc": "bool"}],
                "limit": "1..200",
                "require_time_range": "bool",
            },
            "clarify_question": "string|null",
            "max_steps": "1..3",
            "on_empty": "ASK_CLARIFY|RETRY",
        },
        "few_shots": [{"q": q, "query_model": p} for (q, p) in _FEWSHOTS],
    }

    messages = [LLMMessage(role="user", content=json.dumps(input_block, ensure_ascii=False))]

    raw = await provider.complete(messages, system_prompt=_router_system_prompt())
    try:
        qm = QueryModel.model_validate(_extract_json(raw))
        if forced_time_range:
            if _time_range_rank(forced_time_range) > _time_range_rank(qm.constraints.time_range.value):
                qm = qm.model_copy(update={"constraints": qm.constraints.model_copy(update={"time_range": forced_time_range, "explicit_from": None, "explicit_to": None})})
        plan = _compile_query_model_to_plan(query_model=qm, query=query)
        return plan, raw
    except Exception as exc:
        repair_prompt = (
            "Исправь вывод: верни только валидный JSON объекта QueryModel, без текста. "
            f"Ошибка валидации: {str(exc)[:300]}"
        )
        raw2 = await provider.complete(
            [LLMMessage(role="user", content=raw), LLMMessage(role="user", content=repair_prompt)],
            system_prompt=_router_system_prompt(),
        )
        try:
            qm2 = QueryModel.model_validate(_extract_json(raw2))
            if forced_time_range:
                if _time_range_rank(forced_time_range) > _time_range_rank(qm2.constraints.time_range.value):
                    qm2 = qm2.model_copy(update={"constraints": qm2.constraints.model_copy(update={"time_range": forced_time_range, "explicit_from": None, "explicit_to": None})})
            plan2 = _compile_query_model_to_plan(query_model=qm2, query=query)
            return plan2, raw2
        except Exception as exc2:
            qm_fallback = QueryModel(
                output_shape=QueryOutputShape.ANSWER,
                operation=QueryOperation.SEARCH,
                need_proof=False,
                clarify_question=(
                    "Не смог корректно разобрать запрос для поиска по базе. "
                    "Уточни, пожалуйста: какой чат/период/что именно нужно найти."
                ),
                max_steps=1,
                on_empty=PlanOnEmpty.ASK_CLARIFY,
                notes=f"router_fallback:{str(exc2)[:120]}",
            )
            plan3 = _compile_query_model_to_plan(query_model=qm_fallback, query=query)
            return plan3, json.dumps(qm_fallback.model_dump(), ensure_ascii=False)

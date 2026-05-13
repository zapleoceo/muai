import json
import re
from datetime import datetime

from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider
from app.services.answering_types import Plan


_ROUTER_SYSTEM_PROMPT = (
    "Ты RouterLLM для Telegram-ассистента. "
    "Твоя задача: выбрать стратегию ответа и список инструментов, "
    "вернуть только валидный JSON по схеме Plan. "
    "Никакого текста вокруг JSON. "
    "Всегда добавляй get_recent_dialog(limit=20) для CURRENT_CHAT, если это не опасная команда. "
    "Если пользователь просит 'только личные чаты' — ставь chat_types=['private'] и scope=ALL_CHATS. "
    "Если просит 'только группы' — chat_types=['group','supergroup']. "
    "Если просит 'только каналы' — chat_types=['channel']. "
    "Если пользователь просит ссылку/пруф/исходник сообщения — используй sql_search_messages, и в ответе надо вернуть ссылку(и) из контекста. "
    "Если запрос в форме 'о чем с <имя> говорили' — это почти всегда про личный чат: ставь chat_types=['private'] и scope=ALL_CHATS. "
    "Не вычисляй конкретные timestamps: используй time_range enum. "
    "Если не уверен — задай clarify_question и используй стратегию INFO_ONLY."
)


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
        "- rag_search(scope, query, top_k)\n"
        "- sql_search_messages(scope, query, limit, chat_types?, chat_ids?)\n"
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
        },
    ),
    (
        "О чем с Евочкой говорили вчера?",
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


async def route_query(
    *,
    query: str,
    user_id: int | None,
    chat_id: int,
    language: str = "ru",
    timezone: str = "UTC",
) -> tuple[Plan, str]:
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
        "catalog": _router_tool_catalog(),
        "schema_hint": {
            "strategy": "INFO_ONLY|RAG_SEMANTIC|SQL_DATE_SUMMARY|HYBRID|COMMAND",
            "tools": [{"name": "tool_name", "args": {"k": "v"}}],
            "time_range": "NONE|YESTERDAY|TODAY|LAST_7_DAYS|EXPLICIT",
            "scope": "CURRENT_CHAT|ALL_CHATS",
            "chat_types": ["private|group|supergroup|channel"],
            "chat_ids": [123],
            "explicit_from": "ISO date/datetime | null",
            "explicit_to": "ISO date/datetime | null",
            "clarify_question": "string | null",
        },
        "few_shots": [{"q": q, "plan": p} for (q, p) in _FEWSHOTS],
    }

    messages = [LLMMessage(role="user", content=json.dumps(input_block, ensure_ascii=False))]

    raw = await provider.complete(messages, system_prompt=_ROUTER_SYSTEM_PROMPT)
    try:
        plan = Plan.model_validate(_extract_json(raw))
        return plan, raw
    except Exception as exc:
        repair_prompt = (
            "Исправь вывод: верни только валидный JSON объекта Plan, без текста. "
            f"Ошибка валидации: {str(exc)[:300]}"
        )
        raw2 = await provider.complete(
            [LLMMessage(role="user", content=raw), LLMMessage(role="user", content=repair_prompt)],
            system_prompt=_ROUTER_SYSTEM_PROMPT,
        )
        plan2 = Plan.model_validate(_extract_json(raw2))
        return plan2, raw2

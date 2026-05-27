"""Vera-core registers itself as an HTTP agent so its own self-tools
appear in the unified tool registry (no separate microservice)."""
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.system.tools import HANDLERS

log = logging.getLogger(__name__)
router = APIRouter()

_AGENT_ID = "vera-self"

TOOL_SPECS = [
    {
        "name": "system_deploy",
        "description": (
            "Trigger Vera's own deploy. Dispatches the Deploy workflow on "
            "GitHub Actions which pulls latest master, builds, runs tests, "
            "rolls back on failure. Use when Dima says «задеплой» or after "
            "you've pushed changes via git MCP."
        ),
        "params": [
            {"name": "ref", "type": "string",
             "description": "Branch or tag to deploy. Default: master.",
             "required": False, "default": "master"},
            {"name": "message", "type": "string",
             "description": "Optional reason logged with the deploy.",
             "required": False, "default": ""},
        ],
    },
    {
        "name": "system_status",
        "description": (
            "Vera's own status: git HEAD on server, last 5 GitHub Actions "
            "deploy runs with their conclusion. Use when Dima asks «как "
            "ты?», «что с деплоем?», или для самопроверки после правок."
        ),
        "params": [],
    },
    {
        "name": "vera_set_pref",
        "description": (
            "Toggle Vera's own behaviour preferences. Use when Dima asks "
            "her to change how she behaves. Known keys:\n"
            "  - delete_card_after_decision (bool): if true, card disappears "
            "from chat after Dima taps an action, instead of being edited "
            "with the result.\n"
            "  - execution_recap_in_dm (bool): if true and delete_card_after_"
            "decision is also true, post the action result as a separate DM "
            "message so it isn't lost."
        ),
        "params": [
            {"name": "key", "type": "string",
             "description": "Preference name (see description for list).",
             "required": True},
            {"name": "value", "type": "string",
             "description": "New value. For booleans pass true/false/да/нет/вкл/выкл.",
             "required": True},
        ],
    },
    {
        "name": "vera_get_prefs",
        "description": "Read all current behaviour preferences.",
        "params": [],
    },
    {
        "name": "vera_query_events",
        "description": (
            "ЧИТАЕТ из мозга Веры (Event store + cheap edges в графе), "
            "НЕ из источников (TG/Gmail). Идеально для запросов про "
            "историю: «что писали в чате X», «сообщения от Y за неделю». "
            "Все фильтры AND-комбинируются. Возвращает события с "
            "{id, chat, person, text, direction, occurred_at}."
        ),
        "params": [
            {"name": "source", "type": "string",
             "description": "gmail | telegram", "required": False},
            {"name": "account", "type": "string", "required": False},
            {"name": "folder", "type": "string",
             "description": "TG folder name (case-insensitive)", "required": False},
            {"name": "chat_name", "type": "string", "required": False},
            {"name": "person", "type": "string",
             "description": "имя или username/email", "required": False},
            {"name": "days", "type": "integer",
             "description": "сколько последних дней", "required": False},
            {"name": "since", "type": "string",
             "description": "ISO date YYYY-MM-DD", "required": False},
            {"name": "until", "type": "string",
             "description": "ISO date YYYY-MM-DD", "required": False},
            {"name": "limit", "type": "integer", "required": False},
        ],
    },
    {
        "name": "vera_remember",
        "description": (
            "Запомнить факт/правило/контекст БЕЗ предопределённой схемы. "
            "Используй когда Дима говорит «запомни», «помни», «у меня X = Y», "
            "«не путай A с B», «правило: ...». Создаёт :Memo узел в графе "
            "с произвольным statement-текстом. Не требует чтобы ключ был в "
            "каком-то allowed-list (в отличие от vera_set_pref). Идеально "
            "для контекста: «почта itstep.org — это работа в IT Step "
            "Indonesia, не путать с veranda.my»."
        ),
        "params": [
            {"name": "statement", "type": "string",
             "description": "Свободный текст что запомнить.", "required": True},
            {"name": "scope", "type": "string",
             "description": "Краткий ярлык темы (email_routing, debtors_workflow, etc.)",
             "required": False},
            {"name": "related", "type": "array",
             "description": "Список entity ids (email-адреса, @username, домены) к которым relate-нуть memo.",
             "required": False},
        ],
    },
    {
        "name": "vera_recall",
        "description": (
            "Найти что Вера запомнила. Используй когда нужно вспомнить "
            "правило или факт записанный через vera_remember. Можно по "
            "подстроке (query) или по scope-ярлыку."
        ),
        "params": [
            {"name": "query", "type": "string",
             "description": "Подстрока в statement (case-insensitive).",
             "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
        ],
    },
    {
        "name": "vera_folder_digest",
        "description": (
            "ПРАВИЛЬНЫЙ путь для «что в папке X сегодня». Читает из "
            "МОЗГА (Event store), не из TG. Группирует по чатам, делает "
            "map-reduce LLM-саммари per-chat. Без обращения к Telethon, "
            "без FloodWait. Используй ВСЕГДА вместо telegram_folder_digest "
            "когда нужна история (а не самые свежие сообщения за минуту)."
        ),
        "params": [
            {"name": "folder", "type": "string",
             "description": "название папки", "required": True},
            {"name": "days", "type": "integer",
             "description": "сколько последних дней", "required": False},
        ],
    },
    {
        "name": "bot_delete_message",
        "description": (
            "Delete a specific message in any chat where the bot has rights. "
            "Use for own bot messages (always works) or when admin with "
            "can_delete_messages."
        ),
        "params": [
            {"name": "chat_id", "type": "integer", "description": "Target chat id.", "required": True},
            {"name": "message_id", "type": "integer", "description": "Message id to delete.", "required": True},
        ],
    },
    {
        "name": "bot_delete_forum_topic",
        "description": (
            "Delete an entire forum topic and all its messages. Bot must "
            "have manage_topics + can_delete_messages in the supergroup."
        ),
        "params": [
            {"name": "chat_id", "type": "integer", "description": "Supergroup id.", "required": True},
            {"name": "message_thread_id", "type": "integer", "description": "Topic id.", "required": True},
        ],
    },
    {
        "name": "bot_close_forum_topic",
        "description": "Lock a forum topic (no new messages, content stays).",
        "params": [
            {"name": "chat_id", "type": "integer", "description": "Supergroup id.", "required": True},
            {"name": "message_thread_id", "type": "integer", "description": "Topic id.", "required": True},
        ],
    },
    {
        "name": "bot_wipe_forum",
        "description": (
            "ONE-SHOT: wipe ALL forum topics in a supergroup. Internally "
            "lists topics via vera-telegram, then deletes each. Use this "
            "(NOT a loop of bot_delete_forum_topic) when Dima says «очисти "
            "все темы» / «удали всё» / «снеси форум». Returns deleted_count."
        ),
        "params": [
            {"name": "chat_id", "type": "integer", "description": "Supergroup id.", "required": True},
            {"name": "exclude_general", "type": "boolean",
             "description": "Skip General topic (id=1, undeletable). Default true.",
             "required": False, "default": True},
        ],
    },
    {
        "name": "bot_clear_topic_messages",
        "description": (
            "Sweep recent messages in a forum topic. Best-effort: tries to "
            "delete messages by id backwards from latest. For full wipe of "
            "a topic prefer bot_delete_forum_topic (deletes everything in "
            "one call)."
        ),
        "params": [
            {"name": "chat_id", "type": "integer", "description": "Supergroup id.", "required": True},
            {"name": "message_thread_id", "type": "integer", "description": "Topic id.", "required": True},
            {"name": "limit", "type": "integer", "description": "Max msgs to scan back (default 100).",
             "required": False, "default": 100},
        ],
    },
]


@router.post("/tool/{name}")
async def call_self_tool(name: str, payload: dict | None = None) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        raise HTTPException(404, f"unknown self-tool {name}")
    try:
        result = await handler(**(payload or {}))
    except TypeError as exc:
        return {"ok": False, "error": f"bad args: {exc}"}
    except Exception as exc:
        log.exception("self-tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # Match the contract HTTP tool agents use: {ok, result|error}
    if isinstance(result, dict) and "ok" in result:
        return result
    return {"ok": True, "result": result}


async def register_self_loop() -> None:
    """Register vera-core as an HTTP agent so its self-tools appear in
    collect_tools(). Register once at startup, then heartbeat every 60s."""
    import asyncio
    settings = get_settings()
    payload = {
        "id": _AGENT_ID,
        "name": "vera-self",
        "http_url": "http://localhost:8000",
        "capabilities": ["self_admin"],
        "required_caps": [],
        "tools": TOOL_SPECS,
    }
    headers = {"X-Internal-Secret": settings.internal_secret}
    # Give the FastAPI server a beat to start accepting connections.
    await asyncio.sleep(2)
    registered = False
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                if not registered:
                    r = await c.post("http://localhost:8000/internal/agents/register",
                                     json=payload, headers=headers)
                    if r.status_code == 200:
                        registered = True
                        log.info("vera-self registered: %d tools", len(TOOL_SPECS))
                    else:
                        log.warning("self register %d: %s", r.status_code, r.text[:200])
                else:
                    await c.post("http://localhost:8000/internal/agents/heartbeat",
                                 json={"id": _AGENT_ID}, headers=headers)
        except Exception as exc:
            log.warning("self heartbeat failed: %s", exc)
        await asyncio.sleep(60)

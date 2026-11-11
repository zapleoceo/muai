"""JSON-Schema инструментов для агента (`GET /tools/spec`).

`/actions/send_message` сюда намеренно не входит: агент не должен уметь
говорить от имени владельца."""
from __future__ import annotations

from typing import Any

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "telegram.list_dialogs",
        "description": "List the user's Telegram dialogs (DM, groups, channels). Optional fuzzy query filters by title substring (case-insensitive). Use this to find a chat by name before calling get_participants or get_dialog_history.",
        "params_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Optional title substring"},
                "limit": {"type": "integer", "default": 50, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "telegram.get_chat_info",
        "description": "Get info about a Telegram chat (type, id, title, participants_count). Accepts @username, numeric id, or title substring.",
        "params_schema": {
            "type": "object",
            "properties": {"chat_query": {"type": "string"}},
            "required": ["chat_query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "telegram.get_participants",
        "description": "List members of a Telegram group / supergroup / channel. Requires the user to be a member; admin role required for some private channels.",
        "params_schema": {
            "type": "object",
            "properties": {
                "chat_query": {"type": "string", "description": "@username, numeric id, or title substring"},
                "limit": {"type": "integer", "default": 200, "maximum": 1000},
            },
            "required": ["chat_query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "telegram.get_dialog_history",
        "description": "Fetch recent messages from a specific Telegram dialog. Returns last N messages with text and sender direction.",
        "params_schema": {
            "type": "object",
            "properties": {
                "chat_query": {"type": "string"},
                "limit": {"type": "integer", "default": 30, "maximum": 200},
            },
            "required": ["chat_query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "telegram.find_user",
        "description": "Resolve a Telegram username to a user profile.",
        "params_schema": {
            "type": "object",
            "properties": {"q": {"type": "string", "description": "Username with or without leading @"}},
            "required": ["q"],
            "additionalProperties": False,
        },
    },
]

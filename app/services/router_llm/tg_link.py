import re


def extract_tg_link_ref(query: str) -> dict | None:
    s = str(query or "")
    m = re.search(r"https?://t\.me/c/(?P<internal>\d+)(?:/\d+)?/(?P<msg>\d+)", s)
    if m:
        return {"chat_id": int(f"-100{m.group('internal')}"), "telegram_msg_id": int(m.group("msg"))}
    m = re.search(r"https?://t\.me/(?P<username>[A-Za-z0-9_]{3,})(?:/\d+)?/(?P<msg>\d+)", s)
    if m:
        return {"chat_username": m.group("username"), "telegram_msg_id": int(m.group("msg"))}
    return None


def build_plan_for_tg_ref(ref: dict) -> dict:
    args: dict = {"telegram_msg_id": int(ref["telegram_msg_id"])}
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

"""Evaluators for trigger predicates. Each predicate is a JSON object
like {"from_contains": "boss@company"}; this module decides if an event
matches a given predicate."""

from typing import Any


def matches(predicate: dict[str, Any] | None, event_payload: dict[str, Any]) -> bool:
    """True if event_payload matches every key in predicate.
    None / empty predicate matches anything (catch-all trigger)."""
    if not predicate:
        return True
    for key, expected in predicate.items():
        if not _check(key, expected, event_payload):
            return False
    return True


def _check(key: str, expected: Any, event: dict) -> bool:
    # Gmail-ish
    if key == "from_contains":
        return str(expected).lower() in (event.get("from") or "").lower()
    if key == "subject_matches":
        return str(expected).lower() in (event.get("subject") or "").lower()
    if key == "has_label":
        return expected in (event.get("labels") or [])
    if key == "has_attachment":
        return bool(event.get("has_attachment")) == bool(expected)
    if key == "is_unread":
        return bool(event.get("is_unread")) == bool(expected)
    if key == "body_keyword":
        return str(expected).lower() in (event.get("body") or "").lower()

    # Telegram-ish
    if key == "from_user":
        return str(expected).lower() in (event.get("from_user") or "").lower()
    if key == "in_chat":
        return str(event.get("chat_id") or "") == str(expected) or \
               str(expected).lower() in (event.get("chat_title") or "").lower()
    if key == "mentions_me":
        return bool(event.get("mentions_me")) == bool(expected)
    if key == "keyword":
        return str(expected).lower() in (event.get("text") or "").lower()
    if key == "media_type":
        return event.get("media_type") == expected

    # Numeric
    if key == "amount_gt":
        return float(event.get("amount") or 0) > float(expected)
    if key == "amount_lt":
        return float(event.get("amount") or 0) < float(expected)

    # Unknown — fail closed
    return False


# Per-source declaration of which predicate keys are valid. Used by dashboard
# UI to show only relevant options.
SUPPORTED_BY_SOURCE: dict[str, list[dict]] = {
    "gmail": [
        {"key": "from_contains",   "label": "От: содержит",         "input": "string"},
        {"key": "subject_matches", "label": "Тема: содержит",       "input": "string"},
        {"key": "has_label",       "label": "С Gmail-меткой",        "input": "string"},
        {"key": "has_attachment",  "label": "С вложением",           "input": "boolean"},
        {"key": "is_unread",       "label": "Непрочитанное",         "input": "boolean"},
        {"key": "body_keyword",    "label": "В теле есть слово",    "input": "string"},
    ],
    "telegram": [
        {"key": "from_user",   "label": "От пользователя",       "input": "string"},
        {"key": "in_chat",     "label": "В чате (id или имя)",   "input": "string"},
        {"key": "mentions_me", "label": "Упомянули меня",        "input": "boolean"},
        {"key": "keyword",     "label": "Сообщение содержит",    "input": "string"},
        {"key": "media_type",  "label": "Тип медиа (photo/voice…)", "input": "string"},
    ],
    "bank": [
        {"key": "amount_gt",  "label": "Сумма больше",  "input": "number"},
        {"key": "amount_lt",  "label": "Сумма меньше",  "input": "number"},
    ],
}

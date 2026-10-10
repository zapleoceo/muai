"""Проверка входа office intake: поля источника, хеш полезной нагрузки, курсор выдачи."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from vera_shared.redact import redact_secrets


class InvalidInput(ValueError):
    """Тело разобрано, но поля не проходят проверку (HTTP 422)."""


ORIGIN_KEYS = frozenset({"channel", "chat", "thread", "message", "from_id"})
RECIPIENT_KEYS = frozenset({"channel", "chat", "reply_to", "thread"})
CONTEXT_KEYS = frozenset({"reply_to"})
REPLY_KEYS = frozenset({"id", "user", "text", "media"})
ATTACH_KEYS = frozenset({"type", "file_id", "file_unique_id", "mime", "size", "duration",
                         "w", "h", "url", "url_expires_at"})
ATTACH_INT_KEYS = frozenset({"size", "duration", "w", "h"})
#: подписанная ссылка живёт недолго и меняется между повторами — в хеш не входит
VOLATILE_ATTACH_KEYS = frozenset({"url", "url_expires_at"})
MAX_ATTACHMENTS = 10
MAX_REPLY_TEXT = 2000
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_TITLE = 200
MAX_BODY = 8000
MAX_LIMIT = 100


def clean_ids(client_task_id: str, causation_id: str | None) -> None:
    if not isinstance(client_task_id, str) or not ID_RE.match(client_task_id):
        raise InvalidInput("client_task_id must match [A-Za-z0-9._:-]{1,128}")
    if causation_id is not None and (
            not isinstance(causation_id, str) or not ID_RE.match(causation_id)):
        raise InvalidInput("causation_id must match [A-Za-z0-9._:-]{1,128}")


def clean_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise InvalidInput(f"{name} must be 1..{limit} characters")
    return value.strip()


def clean_map(value: Any, name: str, allowed: frozenset[str], *, required: bool = True,
              max_len: int = 256) -> dict[str, str | int]:
    """Только известные ключи; null отвергается (клиент опускает отсутствующий ключ)."""
    if value is None and not required:
        return {}
    if not isinstance(value, dict) or (required and not value) or set(value) - allowed:
        raise InvalidInput(f"{name} must be an object with keys from "
                           f"{', '.join(sorted(allowed))}")
    for item in value.values():
        if isinstance(item, bool) or not isinstance(item, str | int) or len(str(item)) > max_len:
            raise InvalidInput(f"{name} values must be non-null short strings or integers")
    return dict(value)


def clean_context(value: Any) -> dict[str, Any]:
    """context.reply_to {id,user,text,media}; text режется и вычищается от секретов."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - CONTEXT_KEYS:
        raise InvalidInput("context must be an object with key reply_to")
    if "reply_to" not in value:
        return {}
    reply = clean_map(value["reply_to"], "context.reply_to", REPLY_KEYS, max_len=20_000)
    if "text" in reply:
        reply["text"] = redact_secrets(str(reply["text"]))[:MAX_REPLY_TEXT]
    return {"reply_to": reply}


def clean_attachments(value: Any) -> list[dict[str, str | int]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise InvalidInput(f"attachments must be a list of at most {MAX_ATTACHMENTS} objects")
    clean = []
    for item in value:
        att = clean_map(item, "attachment", ATTACH_KEYS, max_len=2048)
        if "type" not in att or "file_id" not in att:
            raise InvalidInput("attachment needs type and file_id")
        for key in ATTACH_INT_KEYS & set(att):
            if not isinstance(att[key], int) or att[key] < 0:
                raise InvalidInput(f"attachment {key} must be a non-negative integer")
        clean.append(att)
    return clean


def stable_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in a.items() if k not in VOLATILE_ATTACH_KEYS} for a in attachments]


def payload_hash(title: str, body: str, origin: dict[str, Any], recipient: dict[str, Any],
                 context: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
    """Канонический JSON по присутствующим ключам; url вложений и causation_id не входят."""
    raw = json.dumps([title, body, origin, recipient, context, stable_attachments(attachments)],
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse_since(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except ValueError:
        raise InvalidInput("updated_since must be an ISO-8601 timestamp") from None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def clean_limit(value: int | str | None) -> int:
    if value is None or value == "":
        return 50
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise InvalidInput("limit must be an integer") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidInput(f"limit must be 1..{MAX_LIMIT}")
    return limit


def encode_cursor(at: datetime, task_id: str) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{task_id}".encode()).decode()


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        at, _, task_id = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        if not task_id:
            raise ValueError
        return datetime.fromisoformat(at), task_id
    except (ValueError, UnicodeDecodeError):
        raise InvalidInput("cursor is not valid") from None

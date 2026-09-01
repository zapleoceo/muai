"""Сообщение Slack → спецификация строки events. Чистая функция, без сети и БД.

Контракт авторства (docs/sources.md): первая строка content_text — `Author:`,
в metadata обязаны быть author_role и author_label. Для Slack авторство
однозначное: `message.user` сравнивается с id владельца из `auth.test`.

Разметку Slack (`<@U123>`, `<#C1|general>`, `<https://…|текст>`) разворачиваем
здесь же: без этого и выжимка, и поиск по мозгу работали бы по мусору.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from vera_shared.timeutil import utc_naive_now

MAX_CONTENT_LEN = 8000

#: служебные записи канала — тот же класс шума, что `updateCard` с одной сменой
#: `pos` у Trello: очередь триажа они бы раздули ни за чем.
NOISE_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "channel_convert_to_private",
    "group_join", "group_leave", "group_topic", "group_purpose", "group_name",
    "pinned_item", "unpinned_item", "bot_add", "bot_remove", "tombstone",
    "huddle_thread", "reminder_add", "app_conversation_join",
})

_USER_REF = re.compile(r"<@([UBW][A-Z0-9]+)(?:\|([^>]*))?>")
_CHANNEL_REF = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")
_LINK_REF = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
_SPECIAL_REF = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")


def parse_ts(ts: str | None) -> datetime:
    """`ts` Slack («1756123456.001200») → наивный UTC, как во всех источниках."""
    try:
        seconds = float(str(ts or "").split(".")[0] or 0)
    except ValueError:
        return utc_naive_now()
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


def unwrap(text: str, names: dict[str, str]) -> str:
    """Развернуть разметку Slack в читаемый текст."""
    def user(m: re.Match) -> str:
        return "@" + (m.group(2) or names.get(m.group(1)) or m.group(1))

    def channel(m: re.Match) -> str:
        return "#" + (m.group(2) or m.group(1))

    def link(m: re.Match) -> str:
        label = (m.group(2) or "").strip()
        return f"{label} ({m.group(1)})" if label else m.group(1)

    out = _USER_REF.sub(user, text or "")
    out = _CHANNEL_REF.sub(channel, out)
    out = _LINK_REF.sub(link, out)
    out = _SPECIAL_REF.sub(lambda m: "@" + m.group(1), out)
    return out.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def is_noise(message: dict[str, Any]) -> bool:
    """Служебная запись или запись бота — в мозг не идёт."""
    if str(message.get("subtype") or "") in NOISE_SUBTYPES:
        return True
    return bool(message.get("bot_id") or message.get("subtype") == "bot_message")


def _files(message: dict[str, Any]) -> list[str]:
    return [str(f.get("name") or f.get("title") or "файл")
            for f in (message.get("files") or [])]


def message_to_event(
    message: dict[str, Any],
    *,
    channel_id: str,
    channel_name: str,
    channel_kind: str,
    is_private: bool,
    me_id: str,
    account: str,
    names: dict[str, str],
) -> dict[str, Any] | None:
    """Спецификация EventRow, либо None — если сообщение не стоит события."""
    ts = str(message.get("ts") or "")
    if not ts or is_noise(message):
        return None

    author_id = str(message.get("user") or "")
    is_self = bool(author_id) and author_id == me_id
    author_label = "Я" if is_self else (names.get(author_id) or author_id or "участник Slack")
    author_role = "self" if is_self else "counterparty"

    thread_ts = str(message.get("thread_ts") or "")
    in_thread = bool(thread_ts) and thread_ts != ts

    body = unwrap(str(message.get("text") or ""), names).strip()
    attached = _files(message)
    if attached:
        body = (body + "\n" if body else "") + "[файлы] " + ", ".join(attached)
    reactions = [str(r.get("name")) for r in (message.get("reactions") or [])]
    if not body and not reactions:
        return None

    where = ("ЛС" if channel_kind == "im" else f"#{channel_name}")
    content = (
        f"Author: {author_label} [{author_role}]\n"
        f"Where: {where}{' (тред)' if in_thread else ''}\n"
        f"---\n{body}"
    )[:MAX_CONTENT_LEN]

    hints: list[dict[str, Any]] = []
    if author_id and not is_self:
        hints.append({"type": "person", "identifier": f"user:{author_id}",
                      "name": author_label})

    return {
        "source": "slack",
        "source_event_id": f"{channel_id}:{ts}",
        "account": account,
        "category": "thread" if in_thread else channel_kind,
        "content_text": content,
        "occurred_at": parse_ts(ts),
        "entity_hints": hints,
        "metadata_": {
            "author_role": author_role,
            "author_label": author_label,
            # sender_id — общая с telegram/instagram форма: на неё смотрит
            # ingest.authorship, когда ищет алиас автора для графа.
            "sender_id": author_id or None,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "channel_kind": channel_kind,
            "is_private": is_private,
            "thread_ts": thread_ts or None,
            "in_thread": in_thread,
            "ts": ts,
            "reactions": reactions,
            "files": attached,
        },
    }

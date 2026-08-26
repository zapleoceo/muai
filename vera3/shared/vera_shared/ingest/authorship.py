"""Кто автор события — таблицей по источнику, а не цепочкой `if`.

До этого решение жило в graph/rel_extract.py тремя ветками `if source == …`,
а `return` в конце приписывал событие владельцу. Для источника, у которого
ветки нет, это означало тихую порчу графа: все чужие реплики повисали на Диме.
Trello так и жил; Slack жил бы так же. Забыть строку в таблице заметно —
забыть ветку в конце функции нет.

Три исхода, и все три названы:

* кортеж `(alias source, alias identifier)` — автор известен, ищем его алиас;
* `OWNER` — автор владелец (исходящее сообщение или «свой» источник);
* `None` — источник знает про чужое авторство, но конкретного автора не достал.
  Тогда связь скипается: пустая связь лучше связи, повешенной на кого попало.
"""
from __future__ import annotations

from collections.abc import Callable
from email.utils import parseaddr
from typing import Any


class _Owner:
    """Единственное значение — sentinel OWNER. Отдельный тип, чтобы `is` читался."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "OWNER"


OWNER = _Owner()

Author = tuple[str, str] | _Owner | None
AuthorResolver = Callable[[dict[str, Any]], Author]


def _sent_by_owner(meta: dict[str, Any]) -> bool:
    """Исходящее. `direction` — у telegram/gmail/instagram, `author_role` — у остальных
    (контракт авторства, docs/sources.md)."""
    if meta.get("direction") == "sent":
        return True
    return meta.get("author_role") == "self"


def _peer_id(meta: dict[str, Any], alias_source: str) -> Author:
    """Алиас вида `user:<id>` — общая форма telegram / instagram / slack."""
    if _sent_by_owner(meta):
        return OWNER
    sender = meta.get("sender_id")
    if sender in (None, ""):
        return None
    return (alias_source, f"user:{sender}")


def _telegram(meta: dict[str, Any]) -> Author:
    return _peer_id(meta, "telegram")


def _instagram(meta: dict[str, Any]) -> Author:
    return _peer_id(meta, "instagram")


def _slack(meta: dict[str, Any]) -> Author:
    return _peer_id(meta, "slack")


def _gmail(meta: dict[str, Any]) -> Author:
    if _sent_by_owner(meta):
        return OWNER
    _, addr = parseaddr(str(meta.get("from") or ""))
    addr = addr.strip().lower()
    return ("gmail", addr) if addr else None


def _trello(meta: dict[str, Any]) -> Author:
    if _sent_by_owner(meta):
        return OWNER
    username = str(meta.get("author_username") or "").strip()
    return ("trello", username) if username else None


#: источник → как определить автора его события.
#: Источника здесь нет = автор всегда владелец. Так и должно быть для «своих»
#: источников: vera_chat, vera_memory, perplexity, voice, claude.
AUTHOR_RESOLVERS: dict[str, AuthorResolver] = {
    "telegram": _telegram,
    "gmail": _gmail,
    "instagram": _instagram,
    "trello": _trello,
    "slack": _slack,
}


def resolve_author(source: str, meta: dict[str, Any]) -> Author:
    """Автор события источника `source` по его метаданным."""
    resolver = AUTHOR_RESOLVERS.get(source)
    if resolver is None:
        return OWNER
    return resolver(meta or {})

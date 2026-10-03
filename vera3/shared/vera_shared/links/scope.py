"""Область действия прозвища — чистые функции, без базы.

«ДА» заглавными в рабочем чате — человек, в туристическом — слово «да». Прозвище
засчитывается только там, где область (`scope_kind`, миграция 043) это разрешает;
решение принимается по фактам о чате (`ChatContext`) и сильным контактам человека.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

WORK, CONTACTS, CHATS, GLOBAL = "work", "contacts", "chats", "global"
SCOPE_KINDS = (WORK, CONTACTS, CHATS, GLOBAL)
#: Группа без рабочей метки считается «кругом знакомых» человека, если в ней пишут
#: хотя бы столько его сильных контактов.
MIN_STRONG_IN_GROUP = 2


@dataclass(frozen=True)
class ChatContext:
    """Что известно о месте, где написано событие."""
    chat_key: str | None = None
    is_work: bool = False
    dm_partner: int | None = None          # собеседник личного чата (не владелец)
    participants: frozenset[int] = field(default_factory=frozenset)
    #: Второй круг: сильные контакты автора. К нему обращаемся, только если в первом
    #: (участники чата / собеседник лички / владелец) никто не подошёл.
    extended: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class NicknameRule:
    entity_id: int
    token: str
    case_sensitive: bool = True
    scope_kind: str = WORK
    scope_ids: tuple[str, ...] = ()


def in_scope(rule: NicknameRule, ctx: ChatContext, strong_contacts: frozenset[int]) -> bool:
    """Засчитывать ли прозвище в этом чате."""
    if rule.scope_kind == GLOBAL:
        return True
    if rule.scope_kind == CHATS:
        return ctx.chat_key is not None and ctx.chat_key in rule.scope_ids
    work = ctx.is_work or (ctx.dm_partner is not None and ctx.dm_partner in strong_contacts)
    if rule.scope_kind == WORK:
        return work
    return work or len(ctx.participants & strong_contacts) >= MIN_STRONG_IN_GROUP


def token_pattern(rule: NicknameRule) -> re.Pattern[str]:
    """Отдельное слово; регистр — по `case_sensitive`."""
    flags = 0 if rule.case_sensitive else re.IGNORECASE
    return re.compile(rf"(?<!\w){re.escape(rule.token)}(?!\w)", flags)

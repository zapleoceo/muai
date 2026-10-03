"""Данные для контекста событий: представление события, чтение алиасов, чатов, участников.

Автор и получатель определяются по алиасам (`telegram user:<id>`, адрес gmail). Сборка
контекста — `links.context.ContextBuilder`; здесь функции без состояния и SELECT'ы.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session
from vera_shared.graph.connection_model import is_established
from vera_shared.graph.pair_stats import WORK_PROJECTS, partner_stats
from vera_shared.links.scope import ChatContext
from vera_shared.projects.rules import chat_key as canonical_chat_key

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
_PRIVATE = ("user", "private")
#: Источники с автором-человеком по алиасу. voice и прочие строятся иначе.
CHAT_SOURCES = ("telegram", "slack", "instagram")
ALIAS_SOURCES = (*CHAT_SOURCES, "gmail")
#: Больше участников — площадка, а не круг знакомых (то же, что `MAX_CHAT_AUTHORS`).
MAX_CIRCLE_MEMBERS = 60


@dataclass(frozen=True)
class EventView:
    id: int
    source: str
    text: str
    metadata: dict[str, Any]
    extra: dict[str, Any] | None = None       # content_extra (стенограмма созвона)
    transcript: str | None = None
    occurred_at: Any = None
    hidden: bool = False                      # скрытое событие связей не получает


@dataclass(frozen=True)
class EventFacts:
    ctx: ChatContext = field(default_factory=ChatContext)
    author: int | None = None
    recipients: tuple[int, ...] = ()


def addresses_of(value: Any) -> list[str]:
    return [a.lower() for a in _EMAIL.findall(str(value or ""))]


def sender_key(view: EventView) -> tuple[str, str] | None:
    if view.source == "gmail":
        found = addresses_of(view.metadata.get("from"))
        return ("gmail", found[0]) if found else None
    sender = view.metadata.get("sender_id")
    return (view.source, f"user:{sender}") if sender not in (None, "") else None


def chat_id(view: EventView) -> str | None:
    key = "channel_id" if view.source == "slack" else "chat_id"
    value = view.metadata.get(key)
    return None if value in (None, "") else str(value)


def chat_key(raw_chat_id: str) -> str:
    """Ключ чата в `project_membership`: модуль id без -100-префикса супергрупп."""
    try:
        return str(canonical_chat_key(raw_chat_id))
    except ValueError:
        return raw_chat_id


def is_private(view: EventView) -> bool:
    if view.source == "slack":
        return view.metadata.get("channel_kind") == "im"
    return view.source in ("telegram", "instagram") and view.metadata.get("chat_type") in _PRIVATE


def wanted_aliases(views: list[EventView]) -> set[tuple[str, str]]:
    wanted: set[tuple[str, str]] = set()
    for v in views:
        if key := sender_key(v):
            wanted.add(key)
        if v.source == "gmail":
            wanted |= {("gmail", a) for a in addresses_of(v.metadata.get("to"))}
        elif v.source in CHAT_SOURCES and is_private(v) and v.source != "slack" and (c := chat_id(v)):
            wanted.add((v.source, f"user:{c}"))
    return wanted


async def alias_map(keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], int]:
    wanted = sorted({k[1] for k in keys})
    if not wanted:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT source, identifier, entity_id FROM entity_aliases "
                 "WHERE source IN :src AND identifier IN :ids")
            .bindparams(bindparam("src", expanding=True), bindparam("ids", expanding=True)),
            {"src": list(ALIAS_SOURCES), "ids": wanted})).all()
    return {(r[0], r[1].lower() if r[0] == "gmail" else r[1]): r[2] for r in rows}


async def work_chat_keys() -> dict[str, str]:
    """Рабочие чаты: ключ → проект (при нескольких проектах берётся первый по алфавиту)."""
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT key, min(project) FROM project_membership WHERE kind = 'chat' "
                 "AND project IN :p GROUP BY key")
            .bindparams(bindparam("p", expanding=True)), {"p": list(WORK_PROJECTS)})).all()
    return {r[0]: r[1] for r in rows}


async def members(chats: set[str]) -> dict[str, frozenset[int]]:
    """Текущие участники telegram-групп по `memberships`; большие группы (публичные
    площадки) не круг знакомых — их участники не берутся."""
    if not chats:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT a.identifier, m.child_entity_id FROM entity_aliases a "
                 "JOIN memberships m ON m.parent_entity_id = a.entity_id AND m.is_current "
                 "WHERE a.source = 'telegram' AND a.identifier IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": [f"chat:{c}" for c in sorted(chats)]})).all()
    grouped: dict[str, set[int]] = {}
    for identifier, child in rows:
        grouped.setdefault(identifier.removeprefix("chat:"), set()).add(child)
    return {c: frozenset(m) for c, m in grouped.items() if len(m) <= MAX_CIRCLE_MEMBERS}


async def established_contacts(entity_id: int | None) -> frozenset[int]:
    """Сильные контакты сущности: пары с устоявшимся общением (`pair_stats`)."""
    if entity_id is None:
        return frozenset()
    stats = await partner_stats(entity_id)
    return frozenset(p for p, st in stats.items() if is_established(st))


async def owner_entity_id() -> int | None:
    """Сущность владельца: алиас `telegram user:<OWNER_TELEGRAM_ID>`."""
    owner = os.environ.get("OWNER_TELEGRAM_ID", "")
    if not owner.strip("0"):
        return None
    async with get_session() as s:
        return (await s.execute(text(
            "SELECT entity_id FROM entity_aliases WHERE source = 'telegram' "
            "AND identifier = :i LIMIT 1"), {"i": f"user:{owner}"})).scalar_one_or_none()


async def chat_authors() -> dict[str, frozenset[int]]:
    """Кто писал в каждом групповом чате ЗА ВСЮ ИСТОРИЮ: `<источник>:<chat_id>` → сущности. Круг
    разговора не должен зависеть от порядка обработки пачек (backfill идёт от новых к старым).
    Чаты с числом авторов больше `MAX_CIRCLE_MEMBERS` — площадки, не круг — не берутся."""
    groups = ("channel", "chat", "supergroup", "group")
    async with get_session() as s:
        tg = (await s.execute(text(
            "SELECT DISTINCT metadata->>'chat_id', metadata->>'sender_id' FROM events "
            "WHERE source = 'telegram' AND metadata->>'chat_type' IN :t")
            .bindparams(bindparam("t", expanding=True)), {"t": list(groups)})).all()
        sl = (await s.execute(text(
            "SELECT DISTINCT metadata->>'channel_id', metadata->>'sender_id' FROM events "
            "WHERE source = 'slack' AND metadata->>'channel_kind' <> 'im'"))).all()
        aliases = (await s.execute(text(
            "SELECT source, identifier, entity_id FROM entity_aliases "
            "WHERE source IN ('telegram', 'slack') AND identifier LIKE 'user:%'"))).all()
    owners = {(src, ident): eid for src, ident, eid in aliases}
    grouped: dict[str, set[int]] = {}
    for source, rows in (("telegram", tg), ("slack", sl)):
        for chat, sender in rows:
            if chat and sender and (eid := owners.get((source, f"user:{sender}"))) is not None:
                grouped.setdefault(f"{source}:{chat}", set()).add(eid)
    return {k: frozenset(v) for k, v in grouped.items() if len(v) <= MAX_CIRCLE_MEMBERS}


async def person_projects() -> dict[str, frozenset[str]]:
    """Telegram id человека → рабочие проекты, где он писал (`project_membership.kind='person'`)."""
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT key, project FROM project_membership WHERE kind = 'person' "
                 "AND project IN :p").bindparams(bindparam("p", expanding=True)),
            {"p": list(WORK_PROJECTS)})).all()
    out: dict[str, set[str]] = {}
    for key, project in rows:
        out.setdefault(key, set()).add(project)
    return {k: frozenset(v) for k, v in out.items()}

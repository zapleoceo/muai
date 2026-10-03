"""Где, кем и кому написаны события пачки: `EventFacts` для построения связей.

Автор и получатель определяются по алиасам (`telegram user:<id>`, адрес gmail). Получатели —
только там, где адресат однозначен: личка Telegram / Slack im и письмо (To). В группе
«все участники чата» не получатели: кто молчал, тот не был адресатом.

Участники чата — те, кто писал в нём в уже обработанных пачках: запрос «все авторы
чата» по 470 тысячам событий индекса не имеет, а для одиночных имён и прозвищ области
`contacts` достаточно осторожной оценки — неизвестный участник просто не даёт связи.
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
from vera_shared.projects.rules import chat_key

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


@dataclass(frozen=True)
class EventFacts:
    ctx: ChatContext = field(default_factory=ChatContext)
    author: int | None = None
    recipients: tuple[int, ...] = ()


def _addresses(value: Any) -> list[str]:
    return [a.lower() for a in _EMAIL.findall(str(value or ""))]


def _sender_key(view: EventView) -> tuple[str, str] | None:
    if view.source == "gmail":
        found = _addresses(view.metadata.get("from"))
        return ("gmail", found[0]) if found else None
    sender = view.metadata.get("sender_id")
    return (view.source, f"user:{sender}") if sender not in (None, "") else None


def _chat_id(view: EventView) -> str | None:
    key = "channel_id" if view.source == "slack" else "chat_id"
    value = view.metadata.get(key)
    return None if value in (None, "") else str(value)


def _chat_key(chat_id: str) -> str:
    """Ключ чата в `project_membership`: модуль id без -100-префикса супергрупп."""
    try:
        return str(chat_key(chat_id))
    except ValueError:
        return chat_id


def is_private(view: EventView) -> bool:
    if view.source == "slack":
        return view.metadata.get("channel_kind") == "im"
    return view.source in ("telegram", "instagram") and view.metadata.get("chat_type") in _PRIVATE


def _wanted_aliases(views: list[EventView]) -> set[tuple[str, str]]:
    wanted: set[tuple[str, str]] = set()
    for v in views:
        if key := _sender_key(v):
            wanted.add(key)
        if v.source == "gmail":
            wanted |= {("gmail", a) for a in _addresses(v.metadata.get("to"))}
        elif v.source in CHAT_SOURCES and is_private(v) and v.source != "slack" and (c := _chat_id(v)):
            wanted.add((v.source, f"user:{c}"))
    return wanted


async def _alias_map(keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], int]:
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


async def _work_chat_keys() -> frozenset[str]:
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT key FROM project_membership WHERE kind = 'chat' AND project IN :p")
            .bindparams(bindparam("p", expanding=True)), {"p": list(WORK_PROJECTS)})).all()
    return frozenset(r[0] for r in rows)


async def _members(chats: set[str]) -> dict[str, frozenset[int]]:
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


class ContextBuilder:
    """Копит участников чатов между пачками одного прогона."""

    def __init__(self) -> None:
        self._seen: dict[str, set[int]] = {}
        self._members: dict[str, frozenset[int]] = {}
        self._partners: dict[int, frozenset[int]] = {}
        self._work: frozenset[str] | None = None
        self.owner: int | None = None
        self._owner_loaded = False

    async def build(self, views: list[EventView]) -> dict[int, EventFacts]:
        if self._work is None:
            self._work = await _work_chat_keys()
        if not self._owner_loaded:
            self.owner, self._owner_loaded = await owner_entity_id(), True
        aliases = await _alias_map(_wanted_aliases(views))
        fresh = {c for v in views if v.source == "telegram" and not is_private(v)
                 and (c := _chat_id(v)) and c not in self._members}
        await self._load_partners({a for v in views if (a := aliases.get(_sender_key(v) or ("", "")))})
        loaded = await _members(fresh)
        self._members.update({c: loaded.get(c, frozenset()) for c in fresh})
        for view in views:
            author = aliases.get(_sender_key(view) or ("", ""))
            chat = _chat_id(view)
            if author is not None and chat and view.source in CHAT_SOURCES:
                self._seen.setdefault(f"{view.source}:{chat}", set()).add(author)
        return {v.id: self._facts(v, aliases) for v in views}

    async def _load_partners(self, authors: set[int]) -> None:
        for author in authors - self._partners.keys():
            self._partners[author] = await established_contacts(author)

    def _facts(self, view: EventView, aliases: dict[tuple[str, str], int]) -> EventFacts:
        author = aliases.get(_sender_key(view) or ("", ""))
        if view.source == "gmail":
            to = tuple(dict.fromkeys(e for a in _addresses(view.metadata.get("to"))
                                     if (e := aliases.get(("gmail", a))) not in (None, author)))
            return EventFacts(author=author, recipients=to)
        chat = _chat_id(view)
        if chat is None:
            return EventFacts(author=author)
        key = f"{view.source}:{chat}"
        if is_private(view):
            partner = (aliases.get((view.source, f"user:{chat}")) if view.source != "slack"
                       else next(iter(sorted(self._seen.get(key, set()) - {self.owner})), None))
            other = self.owner if view.metadata.get("direction") == "received" else partner
            if view.source == "slack":
                other = self.owner if author != self.owner else partner
            recipients = (other,) if other is not None and other != author else ()
            circle = frozenset(e for e in (partner, self.owner) if e is not None)
            return EventFacts(ChatContext(chat_key=key, dm_partner=partner, participants=circle,
                                          extended=self._partners.get(author or -1, frozenset())),
                              author, recipients)
        work = view.source == "slack" or _chat_key(chat) in (self._work or frozenset())
        circle = set(self._seen.get(key, ())) | self._members.get(chat, frozenset())
        if self.owner is not None:      # события приходят из аккаунта владельца: он в каждом чате
            circle.add(self.owner)
        return EventFacts(ChatContext(chat_key=key, is_work=work,
                                      participants=frozenset(circle),
                                      extended=self._partners.get(author or -1, frozenset())),
                          author)

"""Контекст событий пачки: кто автор, кому адресовано, чей круг разговора.

Получатели — только там, где адресат однозначен: личка Telegram / Slack im и письмо (To).
В группе «все участники чата» не получатели: кто молчал, тот не был адресатом. Круг группового
чата — все, кто писал в нём за историю (`chat_authors`, считается один раз и обновляется по
возрасту), плюс члены группы из `memberships`: порядок обработки событий на него не влияет.
"""
from __future__ import annotations

import time

from vera_shared.links.context_data import (
    CHAT_SOURCES,
    EventFacts,
    EventView,
    addresses_of,
    alias_map,
    chat_authors,
    chat_id,
    chat_key,
    established_contacts,
    is_private,
    members,
    owner_entity_id,
    person_projects,
    sender_key,
    wanted_aliases,
    work_chat_keys,
)
from vera_shared.links.scope import ChatContext

__all__ = ["CHAT_SOURCES", "ContextBuilder", "EventFacts", "EventView", "established_contacts",
           "is_private", "owner_entity_id"]

PRELOAD_MAX_AGE_S = 3600.0


class ContextBuilder:
    """Строит `EventFacts` для пачек; долгоживущий (цикл триажа) — обновляет круги по возрасту."""

    def __init__(self) -> None:
        self._authors: dict[str, frozenset[int]] = {}
        self._seen: dict[str, set[int]] = {}
        self._members: dict[str, frozenset[int]] = {}
        self._partners: dict[int, frozenset[int]] = {}
        self._person_projects: dict[str, frozenset[str]] = {}
        self._work: dict[str, str] | None = None
        self._preloaded_at: float | None = None
        self.owner: int | None = None

    async def preload(self, max_age_s: float = PRELOAD_MAX_AGE_S) -> None:
        """Круги чатов и рабочие проекты — один раз, затем по возрасту (дорогой проход по `events`)."""
        if self._preloaded_at is not None and time.monotonic() - self._preloaded_at < max_age_s:
            return
        self._authors = await chat_authors()
        self._work = await work_chat_keys()
        self._person_projects = await person_projects()
        self.owner = await owner_entity_id()
        self._preloaded_at = time.monotonic()

    async def build(self, views: list[EventView]) -> dict[int, EventFacts]:
        await self.preload()
        aliases = await alias_map(wanted_aliases(views))
        fresh = {c for v in views if v.source == "telegram" and not is_private(v)
                 and (c := chat_id(v)) and c not in self._members}
        await self._load_partners({a for v in views if (a := aliases.get(sender_key(v) or ("", "")))})
        loaded = await members(fresh)
        self._members.update({c: loaded.get(c, frozenset()) for c in fresh})
        for view in views:
            author = aliases.get(sender_key(view) or ("", ""))
            chat = chat_id(view)
            if author is not None and chat and view.source in CHAT_SOURCES:
                self._seen.setdefault(f"{view.source}:{chat}", set()).add(author)
        return {v.id: self._facts(v, aliases) for v in views}

    async def _load_partners(self, authors: set[int]) -> None:
        for author in authors - self._partners.keys():
            self._partners[author] = await established_contacts(author)

    def _project_of(self, view: EventView, chat: str) -> str | None:
        return None if view.source == "slack" else (self._work or {}).get(chat_key(chat))

    def _facts(self, view: EventView, aliases: dict[tuple[str, str], int]) -> EventFacts:
        author = aliases.get(sender_key(view) or ("", ""))
        extended = self._partners.get(author or -1, frozenset())
        if view.source == "gmail":
            to = tuple(dict.fromkeys(e for a in addresses_of(view.metadata.get("to"))
                                     if (e := aliases.get(("gmail", a))) not in (None, author)))
            return EventFacts(author=author, recipients=to)
        chat = chat_id(view)
        if chat is None:
            return EventFacts(author=author)
        key = f"{view.source}:{chat}"
        project = self._project_of(view, chat)
        if is_private(view):
            return self._direct(view, chat, key, project, author, aliases, extended)
        circle = (set(self._seen.get(key, ())) | self._authors.get(key, frozenset())
                  | self._members.get(chat, frozenset()))
        if self.owner is not None:      # события приходят из аккаунта владельца: он в каждом чате
            circle.add(self.owner)
        return EventFacts(ChatContext(
            chat_key=key, is_work=view.source == "slack" or project is not None, project=project,
            participants=frozenset(circle), extended=extended), author)

    def _direct(self, view: EventView, chat: str, key: str, project: str | None, author: int | None,
                aliases: dict[tuple[str, str], int], extended: frozenset[int]) -> EventFacts:
        slack = view.source == "slack"
        known = self._seen.get(key, set()) | self._authors.get(key, frozenset())
        partner = (next(iter(sorted(known - {self.owner})), None) if slack
                   else aliases.get((view.source, f"user:{chat}")))
        if slack:
            other = self.owner if author != self.owner else partner
        else:
            other = self.owner if view.metadata.get("direction") == "received" else partner
        recipients = (other,) if other is not None and other != author else ()
        projects = frozenset() if slack else self._person_projects.get(chat, frozenset())
        circle = frozenset(e for e in (partner, self.owner) if e is not None)
        return EventFacts(ChatContext(
            chat_key=key, is_work=project is not None or slack, project=project, dm_partner=partner,
            dm_partner_projects=projects, participants=circle, extended=extended), author, recipients)

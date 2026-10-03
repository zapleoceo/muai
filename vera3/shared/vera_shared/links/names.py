"""Имя → сущность: полное имя глобально, одиночное — только в круге. Чистые функции.

Полное имя («Имя Фамилия») находит человека в любом месте, если оно у одного. Одиночное
(«Дима», «Оля К.») — только среди людей круга разговора и только если подходит один:
в графе десятки Дим, и «первый попавшийся» приписывал чужому человеку чужие слова.
"""
from __future__ import annotations

from collections.abc import Iterable

from vera_shared.graph.dupe_keys import name_key, name_words
from vera_shared.links.matcher import PersonNames
from vera_shared.links.name_forms import name_group


def _first_keys(name: str) -> set[object]:
    words = name_words(name)
    keys: set[object] = {words[0]} if words else set()
    keys |= {("g", g) for w in words if (g := name_group(w)) is not None}
    return keys


class NameResolver:
    def __init__(self, persons: Iterable[PersonNames]) -> None:
        self._names = {p.entity_id: p.name for p in persons}
        self._by_key: dict[str, list[int]] = {}
        for eid, name in self._names.items():
            if key := name_key(name):
                self._by_key.setdefault(key, []).append(eid)

    def full(self, label: str) -> int | None:
        """Единственный человек с таким полным именем (порядок слов и алфавит неважны)."""
        ids = self._by_key.get(name_key(label), [])
        return ids[0] if len(ids) == 1 else None

    def _matching(self, label: str, circle: Iterable[int]) -> set[int]:
        wanted = _first_keys(label)
        return {eid for eid in circle
                if wanted and eid in self._names and _first_keys(self._names[eid]) & wanted}

    def matches(self, label: str, circle: Iterable[int]) -> bool:
        """Подходит ли имя хоть кому-то из круга (в том числе неоднозначно)."""
        return bool(self._matching(label, circle))

    def short(self, label: str, circle: Iterable[int]) -> int | None:
        """Единственный человек КРУГА, у которого есть эта форма имени; иначе None."""
        found = self._matching(label, circle)
        return next(iter(found)) if len(found) == 1 else None

    def resolve(self, label: str, circle: Iterable[int]) -> tuple[int, str] | None:
        """(сущность, 'full'|'short'); одиночное имя — только внутри круга."""
        if (eid := self.full(label)) is not None:
            return eid, "full"
        if (eid := self.short(label, circle)) is not None:
            return eid, "short"
        return None

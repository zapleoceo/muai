"""Фильтр событий по людям, виду и периоду — один для поиска, MCP и чтения.

Условия складываются через AND: `participant_ids=[A, B]` — события, где были И A, И B
(автор, получатель или участник); `mentioned_ids` — где человека упомянули;
`author_ids` — где писал он; `with_owner` — где был владелец. Строки `scope_ok=false`
(прозвище вне области) и связи ниже `min_confidence` не считаются. Фильтр даёт
фрагмент WHERE для запросов к `events` (подзапросы EXISTS по `event_entities`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from vera_shared.links.model import AUTHOR, KIND_SOURCES, MENTIONED, PRESENCE_ROLES

MAX_IDS = 10


class FilterError(ValueError):
    """Фильтр составлен неверно: неизвестный вид, слишком много id, нет владельца."""


@dataclass(frozen=True)
class EventFilter:
    participant_ids: tuple[int, ...] = ()
    mentioned_ids: tuple[int, ...] = ()
    author_ids: tuple[int, ...] = ()
    with_owner: bool = False
    source: str | None = None
    kind: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    min_confidence: float = 0.0
    project: str | None = None
    account: str | None = None

    @property
    def uses_links(self) -> bool:
        return bool(self.participant_ids or self.mentioned_ids or self.author_ids or self.with_owner)

    @property
    def empty(self) -> bool:
        return not (self.uses_links or self.source or self.kind or self.start or self.end
                    or self.project or self.account)


def _exists(alias: str, param: str, roles: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{r}'" for r in roles)
    return (f"EXISTS (SELECT 1 FROM event_entities l WHERE l.event_id = {alias}.id "
            f"AND l.entity_id = :{param} AND l.role IN ({quoted}) AND l.scope_ok "
            "AND l.confidence >= :link_min)")


def build_where(f: EventFilter, owner_id: int | None, alias: str = "events") -> tuple[str, dict[str, Any]]:
    """(условия через AND без ведущего AND, параметры); пустая строка — фильтр пуст."""
    if any(len(ids) > MAX_IDS for ids in (f.participant_ids, f.mentioned_ids, f.author_ids)):
        raise FilterError(f"не больше {MAX_IDS} id в одном списке")
    parts: list[str] = []
    params: dict[str, Any] = {"link_min": f.min_confidence}
    for prefix, ids, roles in (("p", f.participant_ids, PRESENCE_ROLES),
                               ("m", f.mentioned_ids, (MENTIONED,)),
                               ("a", f.author_ids, (AUTHOR,))):
        for i, entity_id in enumerate(ids):
            parts.append(_exists(alias, f"{prefix}{i}", roles))
            params[f"{prefix}{i}"] = entity_id
    if f.with_owner:
        if owner_id is None:
            raise FilterError("владелец не найден в графе: with_owner недоступен")
        parts.append(_exists(alias, "owner", PRESENCE_ROLES))
        params["owner"] = owner_id
    sources = _sources(f)
    if sources == ():
        parts.append("1 = 0")
    elif sources is not None:
        names = [f"s{i}" for i in range(len(sources))]
        parts.append(f"{alias}.source IN ({', '.join(':' + n for n in names)})")
        params.update(dict(zip(names, sources, strict=True)))
    for column, value in (("project", f.project), ("account", f.account)):
        if value:
            parts.append(f"{alias}.{column} = :f_{column}")
            params[f"f_{column}"] = value
    if f.start:
        parts.append(f"{alias}.occurred_at >= :f_start")
        params["f_start"] = f.start
    if f.end:
        parts.append(f"{alias}.occurred_at < :f_end")
        params["f_end"] = f.end
    return " AND ".join(parts), params


def _sources(f: EventFilter) -> tuple[str, ...] | None:
    if f.kind is not None and f.kind not in KIND_SOURCES:
        raise FilterError(f"kind: одно из {', '.join(KIND_SOURCES)}")
    by_kind = KIND_SOURCES.get(f.kind) if f.kind else None
    if f.source and by_kind and f.source not in by_kind:
        return ()
    if f.source:
        return (f.source,)
    return by_kind


def and_clause(f: EventFilter | None, owner_id: int | None, alias: str = "events") -> tuple[str, dict[str, Any]]:
    """То же с ведущим « AND » — для дописывания к готовому WHERE."""
    if f is None or f.empty:
        return "", {}
    where, params = build_where(f, owner_id, alias)
    return (f" AND {where}" if where else ""), params


def to_dict(f: EventFilter) -> dict[str, Any]:
    """Фильтр для JSON (HTTP к brain-search): даты — ISO-строки, пустое опущено."""
    raw = {"participant_ids": list(f.participant_ids), "mentioned_ids": list(f.mentioned_ids),
           "author_ids": list(f.author_ids), "with_owner": f.with_owner, "source": f.source,
           "kind": f.kind, "start": f.start.isoformat() if f.start else None,
           "end": f.end.isoformat() if f.end else None, "min_confidence": f.min_confidence,
           "project": f.project, "account": f.account}
    return {k: v for k, v in raw.items() if v not in (None, [], False, 0.0)}


def from_dict(data: dict[str, Any]) -> EventFilter:
    """Обратное к `to_dict`; неизвестные ключи — FilterError (фильтр не должен молча теряться)."""
    unknown = set(data) - set(EventFilter.__dataclass_fields__)
    if unknown:
        raise FilterError(f"неизвестные поля фильтра: {', '.join(sorted(unknown))}")
    dates = {k: datetime.fromisoformat(data[k]) for k in ("start", "end") if data.get(k)}
    lists = {k: tuple(data.get(k) or ()) for k in ("participant_ids", "mentioned_ids", "author_ids")}
    rest = {k: data[k] for k in ("with_owner", "source", "kind", "min_confidence", "project", "account")
            if k in data}
    return EventFilter(**lists, **dates, **rest)

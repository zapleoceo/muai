"""Набор «вопросов владельца» и их исполнение общими инструментами (только чтение).

Цель связей событий — универсальность: на любой вопрос о прошлом агент отвечает, собирая
несколько ОБЩИХ инструментов, а не вызывая функцию под вопрос. Здесь 12 вопросов разной
формы записаны как цепочки шагов (`Step`) по тем же функциям, что стоят за MCP-инструментами
(`links.read`, фильтры, `entity_context`), с пометкой, отвечал ли на такой вопрос набор
инструментов ДО связей (`before`: yes / partial / no — разбор по контракту старых инструментов).
`run_case` исполняет цепочку (имена людей и темы подставляются словарём `binds`), `search`
в оффлайн-прогоне — те же фильтры + совпадение слов запроса в тексте (ранжирование смыслом —
дело brain-search и здесь не проверяется). Данные — синтетические подстановки, не люди.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from vera_shared.links.filters import EventFilter
from vera_shared.links.read import (
    co_occurrence,
    entity_events,
    event_participants,
    filtered_events,
    mentioning_events,
)

YES, PARTIAL, NO = "yes", "partial", "no"
PERIOD = {"start": datetime(2026, 9, 1), "end": datetime(2026, 10, 1)}


@dataclass(frozen=True)
class Step:
    tool: str                   # filtered | timeline | participants | co_occurrence | mentions
    args: dict[str, Any] = field(default_factory=dict)   # "$owner" — id из binds; "$prev" — id события прошлого шага
    contains: str | None = None  # слова запроса, которые должны быть в тексте события (оффлайн-search)


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    shape: str
    steps: tuple[Step, ...]
    before: str
    before_note: str
    needs_owner: bool = False      # инструменты показывают суть, но ответ требует решения владельца


CASES = (
    Case("call_participants", "Кто был на созвоне, где мы обсуждали {topic}?", "кто / созвон",
         (Step("filtered", {"kind": "call", "with_owner": True}, "{topic}"), Step("participants", {"event": "$prev"})),
         PARTIAL, "search находит созвон, но имена участников — только строки metadata.voices, без сущностей"),
    Case("asked_the_team", "Что {lisa} спрашивала у команды в сентябре?", "что говорил человек / период",
         (Step("timeline", {"entity": "$lisa", "roles": ("author",), "start": "$start", "end": "$end"}),),
         YES, "timeline по алиасу автора отдавал её сообщения"),
    Case("last_talk_about", "Когда я последний раз говорил с {director} про {topic}?", "когда / пара / тема",
         (Step("filtered", {"participant_ids": ("$owner", "$director")}, "{topic}"),),
         PARTIAL, "timeline({director}) видит только его реплики; свои к нему — нет"),
    Case("chats_by_nickname", "В каких чатах обсуждают {director} по инициалам?", "где упоминают",
         (Step("mentions", {"entity": "$director", "via": ("nickname",)}),),
         NO, "упоминания по прозвищу нигде не связывались с человеком"),
    Case("last_call_with", "Когда я последний раз созванивался с {director} и кто ещё был?", "когда / звонок / состав",
         (Step("filtered", {"participant_ids": ("$owner", "$director"), "kind": "call"}),
          Step("participants", {"event": "$prev"})),
         PARTIAL, "только SQL по metadata.voices, и то если голос опознан слушателем"),
    Case("mails_to", "Покажи письма, где {director} был получателем", "письма / адресат",
         (Step("filtered", {"participant_ids": ("$director",), "kind": "email"}),),
         PARTIAL, "SQL по заголовку to строкой, без разбора адресов и алиасов"),
    Case("said_about_me", "Что обо мне говорили в рабочих чатах в сентябре?", "упоминания обо мне",
         (Step("timeline", {"entity": "$owner", "roles": ("mentioned",), "start": "$start", "end": "$end"}),),
         NO, "по полному имени владельца почти ничего: в чатах пишут «Дима», «Дим»"),
    Case("unnamed_speaker", "Кто такой «Собеседник 2» на созвоне про {topic}?", "кто / неопознанный голос",
         (Step("filtered", {"kind": "call"}, "{topic}"), Step("participants", {"event": "$prev"})),
         NO, "ярлык виден, назвать его нечем; теперь он перечислен отдельно и называется voice_speaker_set",
         needs_owner=True),
    Case("assigned_in_september", "Что {director} поручал в сентябре?", "что говорил человек / период",
         (Step("timeline", {"entity": "$director", "roles": ("author",), "start": "$start", "end": "$end"}),),
         YES, "timeline по алиасу автора"),
    Case("together_with", "О чём мы говорили с {lisa} и {oleg} вместе?", "совместное участие",
         (Step("filtered", {"participant_ids": ("$lisa", "$oleg")}),),
         NO, "нет связи «были оба»: чаты по алиасам не выводились"),
    Case("first_mention_near", "Когда впервые упоминали {topic} рядом с {oleg}?", "когда / тема / человек",
         (Step("filtered", {"mentioned_ids": ("$oleg",)}, "{topic}"),),
         PARTIAL, "поиск по теме без привязки к человеку; упоминания {oleg} не связаны"),
    Case("role_and_basis", "Какая у меня роль с {director} и на чём это основано?", "роль / обоснование",
         (Step("co_occurrence", {"a": "$owner", "b": "$director"}),),
         PARTIAL, "в карточке роль без доказательств (вывод по истории переписки — отдельная ветка pair_roles)"),
)


@dataclass
class CaseResult:
    case: Case
    answerable: bool
    steps: list[dict[str, Any]] = field(default_factory=list)


def _resolve(args: dict[str, Any], binds: dict[str, Any], prev: int | None) -> dict[str, Any]:
    def one(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            return prev if value == "$prev" else binds[value[1:]]
        return value
    return {k: (tuple(one(v) for v in val) if isinstance(val, tuple) else one(val)) for k, val in args.items()}


def _words(contains: str | None, binds: dict[str, Any]) -> tuple[str, ...]:
    return tuple(contains.format(**binds).split()) if contains else ()


async def _step(step: Step, binds: dict[str, Any], prev: int | None) -> tuple[Any, int | None]:
    a = _resolve(step.args, binds, prev)
    if step.tool == "filtered":
        keys = ("participant_ids", "mentioned_ids", "author_ids", "with_owner", "kind", "start", "end")
        events, _ = await filtered_events(EventFilter(**{k: a[k] for k in keys if k in a}), 200,
                                          _words(step.contains, binds))
        return events, events[0]["id"] if events else None
    if step.tool == "timeline":
        events = await entity_events(a["entity"], a["start"], a["end"], 50, a["roles"]) or []
        return events, events[0]["id"] if events else None
    if step.tool == "participants":
        found = await event_participants(a["event"]) if a["event"] else None
        return found, prev
    if step.tool == "mentions":
        rows = [m for m in await mentioning_events(a["entity"], 50) if m["via"] in a["via"]]
        return rows, rows[0]["id"] if rows else None
    return await co_occurrence(a["a"], a["b"]), prev


def _non_empty(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("events") or result.get("participants"))
    return bool(result)


async def run_case(case: Case, binds: dict[str, Any]) -> CaseResult:
    """Исполняет цепочку; ответ возможен, если последний шаг дал непустой результат."""
    binds = {**PERIOD, **binds}                 # период по умолчанию; `--start/--end` его заменяют
    out = CaseResult(case, False)
    prev: int | None = None
    result: Any = None
    for step in case.steps:
        result, prev = await _step(step, binds, prev)
        out.steps.append({"tool": step.tool, "non_empty": _non_empty(result)})
    out.answerable = _non_empty(result)
    return out


def after_level(result: CaseResult) -> str:
    """yes / partial / no ПОСЛЕ связей: непустой результат; вопросы, где суть показана, а ответ
    даёт владелец (назвать голос), — partial."""
    if not result.answerable:
        return NO
    return PARTIAL if result.case.needs_owner else YES


def render_question(case: Case, names: dict[str, str]) -> str:
    return case.question.format(**names)

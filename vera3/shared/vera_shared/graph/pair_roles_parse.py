"""Разбор ответа модели по паре: направление, проверка цитат по пакету, отсев шуток.

Роль принимается, только если: предикат из канонического набора, не помечена «только шутка
или ирония», уверенность не ниже `MIN_CONFIDENCE`, и ХОТЯ БЫ ОДНА цитата — дословная подстрока
пакета (нормализация как у `rel_verify`: регистр, пунктуация, пробелы; цитата короче
`MIN_QUOTE_CHARS` = 12 знаков доказательством не считается). Цитаты, которых в пакете нет,
отбрасываются поштучно; роль без единой настоящей цитаты — целиком. Обе стороны иерархии
одновременно невозможны: остаётся более уверенная.

**Самоутверждение.** Роль начальника / родителя, все цитаты которой написал сам «начальник»
(«я твой директор», его поручения без единого ответа второй стороны или третьих лиц), без
подтверждения не принимается: так модель превращала бы одну сторону переписки в доказательство.
Подтверждение — цитата, написанная второй стороной или третьим лицом, либо структурная
(адрес, должность из сигналов), которой нет ни в одном сообщении.

Каждая проверка оставляет след (`pair_roles_trace`): `parse_traced` возвращает его рядом с
ролями, `parse_answer` — прежний результат без следа.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from vera_shared.graph.pair_roles_lexicon import TITLE
from vera_shared.graph.pair_roles_trace import DROPPED, KEPT, QuoteTrace, RoleTrace
from vera_shared.graph.pair_roles_types import (
    A_TO_B,
    B_TO_A,
    BOTH,
    PREDICATES,
    PackMessage,
    RoleFinding,
)
from vera_shared.graph.rel_verify import normalize

MIN_CONFIDENCE = 0.6
MAX_QUOTES = 3
MIN_QUOTE_CHARS = 12
#: Роли, где доказательство, написанное самим «старшим», — самоутверждение.
SELF_ASSERTING = frozenset({"boss_of", "parent_of"})
MAX_ROLES = 4
SYMMETRIC_PREDICATES = frozenset({"coworker_of", "co_founder_of", "friend_of", "spouse_of"})
HIERARCHY = ("boss_of", "parent_of")
_DIRECTION = {"A": A_TO_B, "B": B_TO_A, "both": BOTH}
CONFIRMED = "подтверждена"
SELF_ASSERTED = "самоутверждение"
NOT_APPLICABLE = "не применяется"


class PairRolesFormatError(ValueError):
    """Ответ модели не по схеме."""


def _quote_authors(quote: str, authored: list[tuple[str, str]]) -> set[str]:
    """Кто написал сообщения, содержащие цитату; пусто — цитата структурная (адрес, должность)."""
    needle = normalize(quote)
    return {author for author, text in authored if needle in text}


def _structural(quote: str) -> bool:
    """Цитата-сигнал, а не реплика: адрес почты или должность (имя человека — не сигнал)."""
    return "@" in quote or bool(TITLE.search(quote))


def analyse_quote(quote: Any, haystack: str, authored: list[tuple[str, str]]) -> QuoteTrace:
    text = re.sub(r"\s+", " ", str(quote)).strip()
    needle = normalize(text)
    trace = QuoteTrace(text, needle, short=len(needle) < MIN_QUOTE_CHARS)
    trace.found = not trace.short and needle in haystack
    if trace.found:
        authors = _quote_authors(text, authored)
        trace.authors = tuple(sorted(authors))
        trace.structural = not authors and _structural(text)
    return trace


def valid_quotes(quotes: list[Any], corpus: str) -> tuple[str, ...]:
    """Дословные подстроки пакета; остальное отброшено."""
    haystack = normalize(corpus)
    out: list[str] = []
    for quote in quotes[:MAX_QUOTES + 2]:
        trace = analyse_quote(quote, haystack, [])
        if trace.found and trace.raw not in out:
            out.append(trace.raw)
    return tuple(out[:MAX_QUOTES])


def _self_verdict(finding: RoleFinding, quotes: list[QuoteTrace], have_messages: bool) -> str:
    if finding.predicate not in SELF_ASSERTING or finding.direction == BOTH or not have_messages:
        return NOT_APPLICABLE
    superior = "A" if finding.direction == A_TO_B else "B"
    authors: set[str] = set()
    for quote in quotes:
        if quote.structural:
            return f"{CONFIRMED} (структурная цитата)"
        authors |= set(quote.authors)
    if authors <= {superior}:
        return SELF_ASSERTED
    return f"{CONFIRMED} (цитата другой стороны или третьего лица)"


def _direction(predicate: str, subject: str) -> str | None:
    if predicate in SYMMETRIC_PREDICATES:
        return BOTH
    return _DIRECTION.get(subject) if subject != "both" else None


def _judge(item: Any, haystack: str, authored: list[tuple[str, str]],
           have_messages: bool) -> tuple[RoleFinding | None, RoleTrace]:
    trace = RoleTrace(raw=item)
    if not isinstance(item, dict):
        trace.reason = "элемент ответа не объект"
        return None, trace
    trace.predicate, trace.subject = str(item.get("predicate", "")), str(item.get("subject", ""))
    trace.joke = item.get("joke_or_irony_only") is True
    if trace.joke:
        trace.reason = "помечена моделью как шутка или ирония"
        return None, trace
    direction = _direction(trace.predicate, trace.subject)
    if trace.predicate not in PREDICATES or direction is None:
        trace.reason = "неизвестный предикат или не указана сторона иерархии"
        return None, trace
    try:
        trace.confidence = max(0.0, min(1.0, float(item["confidence"])))
    except (KeyError, TypeError, ValueError):
        trace.reason = "нет числовой уверенности"
        return None, trace
    trace.quotes = [analyse_quote(q, haystack, authored)
                    for q in list(item.get("quotes") or [])[:MAX_QUOTES + 2]]
    good = [q for q in trace.quotes if q.found]
    if trace.confidence < MIN_CONFIDENCE:
        trace.reason = f"уверенность {trace.confidence:.2f} ниже порога {MIN_CONFIDENCE}"
        return None, trace
    if not good:
        trace.reason = f"нет ни одной цитаты, найденной в пакете (от {MIN_QUOTE_CHARS} знаков)"
        return None, trace
    finding = RoleFinding(trace.predicate, direction, round(trace.confidence, 2),
                          str(item.get("rationale", "")).strip()[:600],
                          tuple(dict.fromkeys(q.raw for q in good))[:MAX_QUOTES])
    trace.self_assertion = _self_verdict(finding, good, have_messages)
    if trace.self_assertion == SELF_ASSERTED:
        trace.reason = ("все цитаты написал сам «старший» — нужна цитата второй стороны, "
                        "третьего лица или адрес/должность")
        return None, trace
    return finding, trace


def _drop_rival_hierarchy(findings: list[RoleFinding]) -> tuple[list[RoleFinding], set[int]]:
    best: dict[str, RoleFinding] = {}
    for f in findings:
        if f.predicate in HIERARCHY and (f.predicate not in best
                                         or f.confidence > best[f.predicate].confidence):
            best[f.predicate] = f
    kept = [f for f in findings if f.predicate not in HIERARCHY or best[f.predicate] is f]
    return kept, {id(f) for f in findings} - {id(f) for f in kept}


def parse_traced(raw: str, corpus: str, messages: Iterable[PackMessage] = ()
                 ) -> tuple[list[RoleFinding], str, list[RoleTrace]]:
    """(роли, резюме, след по каждой роли модели). Бросает `PairRolesFormatError`, если это не
    JSON по схеме. `messages` — сообщения пакета: по ним находится автор цитат."""
    messages = list(messages)
    try:
        data = json.loads(raw)
        roles, summary = data["roles"], str(data.get("relationship_summary", "")).strip()
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise PairRolesFormatError("answer is not valid pair_roles JSON") from e
    if not isinstance(roles, list):
        raise PairRolesFormatError("roles must be a list")
    haystack = normalize(corpus)
    authored = [(m.author, normalize(m.text)) for m in messages]
    traces: list[RoleTrace] = []
    candidates: dict[tuple[str, str], tuple[RoleFinding, RoleTrace]] = {}
    for item in roles[:MAX_ROLES * 2]:
        finding, trace = _judge(item, haystack, authored, bool(messages))
        traces.append(trace)
        if finding is None:
            continue
        key = (finding.predicate, finding.direction)
        if key in candidates and candidates[key][0].confidence >= finding.confidence:
            trace.reason = "повтор той же роли с меньшей уверенностью"
            continue
        if key in candidates:
            candidates[key][1].reason = "повтор той же роли с меньшей уверенностью"
        candidates[key] = (finding, trace)
    ordered = sorted(candidates.values(), key=lambda pair: -pair[0].confidence)
    kept, rivals = _drop_rival_hierarchy([f for f, _ in ordered])
    by_id = {id(f): t for f, t in ordered}
    for finding, trace in ordered:
        if id(finding) in rivals:
            trace.reason = "противоположная сторона той же иерархии увереннее"
    final = kept[:MAX_ROLES]
    for finding in final:
        by_id[id(finding)].verdict, by_id[id(finding)].reason = KEPT, "принята"
    for finding in kept[MAX_ROLES:]:
        by_id[id(finding)].reason = f"сверх лимита {MAX_ROLES} ролей"
    for trace in traces:
        if trace.verdict != KEPT:
            trace.verdict = DROPPED
    return final, summary[:300], traces


def parse_answer(raw: str, corpus: str,
                 messages: Iterable[PackMessage] = ()) -> tuple[list[RoleFinding], str]:
    """(роли, резюме) — то же без следа."""
    roles, summary, _ = parse_traced(raw, corpus, messages)
    return roles, summary

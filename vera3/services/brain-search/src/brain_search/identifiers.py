"""Тикет-подобные идентификаторы (SIN-4905, LAM-176) в вопросе.

Запрос идёт в tsquery как OR префиксов: «SIN-4905» превращается в
`sin:* | 4905:*`, а `sin:*` на проде даёт 4863 совпадения, и точные
события тонут за десятым местом. Идентификатор — точный токен, поэтому
его ищем отдельно (identifier_rows.py) и ставим выше любого семантического
ранга.
"""
from __future__ import annotations

import re

#: Префикс ≥2 букв и номер ≥2 цифр: `x-2`, `gpt-4` не тикеты.
_TICKET = re.compile(r"(?<![\w-])([A-Za-z]{2,10})-(\d{2,7})(?![\w-])")
MAX_IDENTIFIERS = 5
#: Похожи по форме, но не тикеты (covid-19, iso-8601, sha-256 …). Список
#: намеренно короткий; пропущенный префикс стоит одного лишнего запроса.
NON_TICKET_PREFIXES = frozenset({
    "utf", "gpt", "covid", "iso", "sha", "md", "rfc", "mp", "ecma", "ansi",
    "cp", "win", "ip", "ipv", "http", "tls", "ssl", "aes", "rsa", "usb", "pdf",
})


def ticket_ids(question: str) -> list[str]:
    """Идентификаторы в верхнем регистре без повторов, в порядке появления."""
    found = (f"{m.group(1).upper()}-{m.group(2)}" for m in _TICKET.finditer(question)
             if m.group(1).lower() not in NON_TICKET_PREFIXES)
    return list(dict.fromkeys(found))[:MAX_IDENTIFIERS]


#: 7–40 hex-символов с цифрой и буквой a-f: не число, не слово.
_HASH = re.compile(r"(?<![0-9A-Za-z-])([0-9a-fA-F]{7,40})(?![0-9A-Za-z])")
MAX_HASH_SUFFIX = 33  # 40 − минимальные 7 символов префикса


def commit_hashes(question: str) -> list[str]:
    """Хэши коммитов (полные или 7+ символов префикса) в нижнем регистре."""
    found = (m.group(1).lower() for m in _HASH.finditer(question)
             if re.search(r"\d", m.group(1)) and re.search(r"[a-fA-F]", m.group(1)))
    return list(dict.fromkeys(found))[:MAX_IDENTIFIERS]


def is_hash(identifier: str) -> bool:
    return "-" not in identifier


def exact_identifiers(question: str) -> list[str]:
    """Тикеты и хэши коммитов из вопроса: всё, что ищется дословно."""
    return (ticket_ids(question) + commit_hashes(question))[:MAX_IDENTIFIERS]


def is_identifier_only(question: str, identifiers: list[str]) -> bool:
    """Вопрос состоит по сути только из идентификатора (без слов вокруг)."""
    if not identifiers:
        return False
    rest = question
    for ident in identifiers:
        rest = re.sub(re.escape(ident), " ", rest, flags=re.IGNORECASE)
    return not re.search(r"[^\W\d_]{3,}", rest)


def identifier_hits(tickets: list[str], texts: dict[int, str | None]) -> dict[str, list[int]]:
    """Какой идентификатор в каких событиях встречается дословно (с границами)."""
    hits: dict[str, list[int]] = {}
    for ticket in tickets:
        if is_hash(ticket):
            pattern = re.compile(
                rf"(?<![0-9a-f]){re.escape(ticket)}[0-9a-f]{{0,{MAX_HASH_SUFFIX}}}(?![0-9a-f])",
                re.IGNORECASE)
        else:
            pattern = re.compile(rf"(?<![\w-]){re.escape(ticket)}(?!\d)", re.IGNORECASE)
        ids = [eid for eid, body in texts.items() if body and pattern.search(body)]
        if ids:
            hits[ticket] = ids
    return hits


NO_EXACT_ANSWER = (
    "Точных упоминаний этого идентификатора среди доступных событий не нашла. "
    "Это ограниченная выборка поиска: она не доказывает, что записи не существует."
)


def identifier_miss_note(identifiers: list[str], hits: dict[str, list[int]]) -> str:
    """Идентификатор спрошен, но дословно не найден: не утверждать «нет записи»."""
    missing = [i for i in identifiers if i not in hits]
    if not missing:
        return ""
    return (f"\n\nТочных упоминаний {', '.join(missing)} в найденных событиях нет. "
            "Не утверждай категорично, что записи нет: скажи «точных упоминаний не "
            "нашла», а похожие события назови только как косвенные.")


def identifier_note(hits: dict[str, list[int]]) -> str:
    """Жёсткое указание синтезу: дословное совпадение есть — «не найдено» запрещено.
    Только для идентификаторов, реально найденных в результатах (hits)."""
    if not hits:
        return ""
    lines = [f"- {ticket}: {', '.join(f'[event:{i}]' for i in ids[:8])}"
             for ticket, ids in hits.items()]
    return ("\n\nТОЧНОЕ СОВПАДЕНИЕ ИДЕНТИФИКАТОРА. Эти идентификаторы из вопроса "
            "найдены дословно в событиях:\n" + "\n".join(lines) +
            "\nНЕЛЬЗЯ отвечать «упоминаний нет / не найдено»; опиши эти события.")

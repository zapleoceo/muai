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


def identifier_hits(tickets: list[str], texts: dict[int, str | None]) -> dict[str, list[int]]:
    """Какой идентификатор в каких событиях встречается дословно (с границами)."""
    hits: dict[str, list[int]] = {}
    for ticket in tickets:
        pattern = re.compile(rf"(?<![\w-]){re.escape(ticket)}(?!\d)", re.IGNORECASE)
        ids = [eid for eid, body in texts.items() if body and pattern.search(body)]
        if ids:
            hits[ticket] = ids
    return hits


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

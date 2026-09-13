"""Шапка события: ингесторы пишут `Author:/From:/Chat:/Date:…\\n---\\n<тело>`.

Отрезать шапку раньше умели по копии в `graph/dedup.py` и `graph/dossiers.py`;
третьим потребителем стал гейт rel-extract (`graph/rel_policy.py`), которому
нельзя искать маркеры отношений в шапке — название чата «Веранда сотрудники»
иначе делает «сотрудником» каждого его участника.
"""
from __future__ import annotations

HEADER_SEPARATOR = "\n---\n"


def message_body(content_text: str | None) -> str:
    """Текст сообщения без шапки ингестора; без шапки — весь текст как есть."""
    if not content_text:
        return ""
    return content_text.split(HEADER_SEPARATOR, 1)[-1]

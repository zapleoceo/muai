"""Автор и направление события для контекста синтеза.

Автор берётся из metadata события (author_label/author_role/direction), а
не из «чей это ящик»: во входящем письме Jira-комментарий написан
коллегой, и модель, не знавшая этого, приписывала его Диме.
"""
from __future__ import annotations

from brain_search.rows import Candidate

_DIRECTION = {"received": "входящее", "sent": "исходящее"}

AUTHORSHIP_RULES = (
    "\nПравила авторства и модальности:\n"
    "- Автор указан в заголовке события (author=…). Приписывай высказывание "
    "именно этому автору. Диме («ты») — только если author_role=self или "
    "направление исходящее; во входящем событии писал другой человек, даже "
    "если письмо пришло Диме.\n"
    "- Сохраняй модальность источника: требование, план, условие, "
    "предложение («не включаем до решения», «нужно», «должен») не превращай в "
    "установленный факт («выключен», «сделано»). Пересказывай глаголом "
    "источника: «требует / предлагает / условие: …».\n"
)


def author_tag(c: Candidate) -> str:
    """`author=<label> (<role>, <direction>)` или пусто, если метаданных нет."""
    label = (c.author_label or "").strip()
    role = (c.author_role or "").strip()
    direction = _DIRECTION.get(c.direction or "", c.direction or "")
    if role == "self" and not label:
        label = "Дима"
    details = ", ".join(x for x in (role, direction) if x)
    if not label and not details:
        return ""
    return f"author={label or 'неизвестен'}" + (f" ({details})" if details else "")

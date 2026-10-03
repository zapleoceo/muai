"""След разбора ответа модели: что именно проверено и почему роль принята или отброшена.

Нужен `infer_pair_roles.py --explain`: пустой список ролей при резюме «Дима — руководитель Лизы»
без причины — тупик для отладки. След собирает сам разбор (`pair_roles_parse.parse_traced`),
логов нет; в основной путь он не влияет.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

KEPT, DROPPED = "kept", "dropped"


@dataclass
class QuoteTrace:
    raw: str
    normalized: str
    found: bool = False
    short: bool = False
    authors: tuple[str, ...] = ()       # A / B / X — кто написал сообщения с цитатой
    structural: bool = False            # адрес или должность без автора

    def as_dict(self) -> dict[str, Any]:
        where = "слишком короткая" if self.short else (
            "не найдена в пакете" if not self.found else "найдена")
        who = ("структурная (адрес/должность)" if self.structural else
               "авторы: " + (",".join(self.authors) or "нет"))
        return {"quote": self.raw, "normalized": self.normalized, "status": where, "who": who}


@dataclass
class RoleTrace:
    raw: Any                            # роль как её вернула модель
    predicate: str = ""
    subject: str = ""
    confidence: float | None = None
    joke: bool = False
    quotes: list[QuoteTrace] = field(default_factory=list)
    self_assertion: str = ""            # '', 'не применяется', 'подтверждена', 'самоутверждение'
    verdict: str = DROPPED
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"predicate": self.predicate, "subject": self.subject, "confidence": self.confidence,
                "joke_or_irony_only": self.joke, "quotes": [q.as_dict() for q in self.quotes],
                "self_assertion": self.self_assertion or None, "verdict": self.verdict,
                "reason": self.reason}


def explain_payload(result: Any) -> list[dict[str, Any]]:
    """Читаемый след `PairInference.trace` для CLI: сырые роли модели и исход каждой проверки."""
    return [{"model_returned": t.raw, **t.as_dict()} for t in result.trace]

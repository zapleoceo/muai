"""Правила срочной просьбы голосом — в одном месте: порог, маршрут, сроки, тексты.

Порог `CONFIRM_BELOW` тот же, что в слушателе (`vera_listener.command_intake`):
там же формула уверенности и почему 0.75. Сервер решает по нему сам, а не
верит флагу слушателя: старый слушатель флага не пришлёт, а порог иногда
придётся двигать без выкатки ноутбука.
"""
from __future__ import annotations

import os
from datetime import timedelta

CONFIRM_BELOW = 0.75
#: Признаки сомнения от слушателя, при которых задача — только после «Да».
DOUBTS = frozenset({"mid_sentence", "short"})

#: «Это ты сказал?» ждёт ответа столько; потом отмена, без исполнения.
CONFIRM_TTL = timedelta(minutes=10)
#: Нет claim за столько — эскалация dot; за столько — ещё напоминание владельцу.
ESCALATE_AFTER = timedelta(minutes=5)
REMIND_AFTER = timedelta(minutes=20)

AGENT = "vera"
RESPONSIBLE = "Claude"
ESCALATE_TO = "dot"
DEFAULT_PROJECT = "Vera"

#: Правило маршрутизации — одно: подстрока app/window → проект комнаты.
#: Первое совпадение выигрывает; ничего не подошло — DEFAULT_PROJECT.
PROJECT_RULES: tuple[tuple[str, str], ...] = (
    ("itstep", "itstep"),
    ("sintegrum", "itstep"),
    ("jira", "itstep"),
    ("veranda", "veranda"),
    ("веранда", "veranda"),
)

SAFETY_NOTE = ("Срочность не снимает подтверждений: деньги, необратимые действия, "
               "доступы — по общим правилам. Без отдельного разрешения — только "
               "безопасный сбор контекста в уже выданных пределах.")


def help_room() -> str:
    """Комната задач. В тестах — `voice-test`: боевую `main` они не трогают."""
    return os.environ.get("VOICE_HELP_ROOM", "main")


def project_for(app: str | None, window_title: str | None) -> str:
    where = f"{app or ''} {window_title or ''}".lower()
    for needle, project in PROJECT_RULES:
        if needle in where:
            return project
    return DEFAULT_PROJECT


def needs_confirmation(confidence: float, doubts: list[str]) -> bool:
    return confidence < CONFIRM_BELOW or bool(DOUBTS.intersection(doubts))


def help_task_id(command_id: str) -> str:
    return f"help-{command_id}"

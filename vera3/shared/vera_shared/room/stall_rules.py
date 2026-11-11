"""Правило «задача не двигается»: чистая функция, время приходит аргументом.

Живая задача в каждый момент держится одним из четырёх: живой арендой, вопросом
владельцу, паузой владельца или датированным ожиданием. Нет ничего из этого дольше
`STALL_AFTER` — задача зависла и попадает на пульт владельца (`stall_board`).
Кнопка «Ждать» убирает её с пульта на `SNOOZE`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

STALL_AFTER = timedelta(days=2)
SNOOZE = timedelta(days=3)
ACTIVE_STATUSES = ("open", "blocked")
STATUS_RU = {"open": "открыта, никто не взял", "blocked": "заблокирована"}
NOTE_CHARS = 280
TITLE_CHARS = 160


@dataclass(frozen=True)
class StallFacts:
    status: str
    live_lease: bool
    owner_blocked: bool
    recovery_hold: bool
    attested: bool
    waiting_until: datetime | None
    last_activity: datetime
    snooze_until: datetime | None


def is_stalled(f: StallFacts, now: datetime) -> bool:
    if f.status not in ACTIVE_STATUSES or f.live_lease or f.owner_blocked:
        return False
    # recover и аттестация — свои маршруты владельца/dot; сторож их не подменяет
    if f.recovery_hold or f.attested:
        return False
    if f.waiting_until is not None and f.waiting_until > now:
        return False
    if f.snooze_until is not None and f.snooze_until > now:
        return False
    return now - f.last_activity >= STALL_AFTER


def short(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def days_word(days: int) -> str:
    tail, tail100 = days % 10, days % 100
    if tail == 1 and tail100 != 11:
        return "день"
    if 2 <= tail <= 4 and not 12 <= tail100 <= 14:
        return "дня"
    return "дней"


def question_text(*, title: str | None, task_id: str, project: str | None, status: str,
                  note: str | None, next_action: str | None, idle: timedelta) -> str:
    days = max(idle.days, 1)
    lines = [f"Стоит {days} {days_word(days)}: {short(title or task_id, TITLE_CHARS)}",
             f"Проект: {project or 'без проекта'}. Сейчас: {STATUS_RU.get(status, status)}."]
    if reason := short(note, NOTE_CHARS):
        lines.append(f"Последняя заметка: {reason}")
    if step := short(next_action, NOTE_CHARS):
        lines.append(f"Следующий шаг по записи: {step}")
    lines.append("Что делаем?")
    return "\n".join(lines)

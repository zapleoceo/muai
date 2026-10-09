"""Что требует внимания в задаче: одна чистая функция для списка, inbox и сторожа.

Состояние считается только из last_progress_at, next_checkpoint_at, lease_until,
waiting_until и status (плюс явные входы: вопрос владельцу, пауза, время захвата).
Продления аренды (heartbeat) и updated_at в расчёте не участвуют: «жива» аренда
не значит «задача движется». Формулировки — только измеримое («нет обновления
3 ч»), без оценок.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

DEFAULT_STALE_WINDOW = timedelta(hours=2)

UNASSIGNED, IN_PROGRESS, STALE_PROGRESS, LEASE_EXPIRED = (
    "unassigned", "in_progress", "stale_progress", "lease_expired")
WAITING, NEEDS_OWNER, BLOCKED, PAUSED, DONE, CANCELLED = (
    "waiting", "needs_owner", "blocked", "paused", "done", "cancelled")
NEEDS_ATTENTION = (UNASSIGNED, STALE_PROGRESS, LEASE_EXPIRED, NEEDS_OWNER)


@dataclass(frozen=True)
class Attention:
    state: str
    label_ru: str
    since: datetime | None
    last_result_at: datetime | None
    last_result_text: str | None


def humanize(delta: timedelta) -> str:
    """«3 ч», «2 ч 15 мин», «1 дн 4 ч»; сокращения не склоняются."""
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} дн {hours} ч" if hours else f"{days} дн"
    if hours:
        return f"{hours} ч {mins} мин" if mins else f"{hours} ч"
    return f"{mins} мин" if mins else "менее минуты"


def _stale_deadline(task: Any, claimed_at: datetime | None) -> datetime | None:
    last, checkpoint = task.last_progress_at, task.next_checkpoint_at
    # контрольная точка, назначенная до последнего прогресса, уже отработана
    if checkpoint is not None and (last is None or checkpoint > last):
        return checkpoint
    base = last or claimed_at
    return base + DEFAULT_STALE_WINDOW if base is not None else None


def attention(task: Any, now: datetime, *, open_question_at: datetime | None = None,
              paused: bool = False, claimed_at: datetime | None = None) -> Attention:
    """task — строка RoomTaskRow или любой объект с теми же атрибутами."""
    def make(state: str, label: str, since: datetime | None) -> Attention:
        return Attention(state, label, since, task.last_progress_at,
                         task.last_progress_text)

    if task.status in (DONE, CANCELLED):
        label = "готово" if task.status == DONE else "отменена"
        return make(task.status, label, task.updated_at)
    if open_question_at is not None:
        return make(NEEDS_OWNER, f"ждёт ответа владельца {humanize(now - open_question_at)}",
                    open_question_at)
    if paused:
        return make(PAUSED, "на паузе", None)
    if task.status == "blocked":
        return make(BLOCKED, "заблокирована", None)
    if not task.lease_holder:
        # у задачи без держателя нет heartbeat'ов: updated_at = момент, когда её отпустили
        return make(UNASSIGNED, f"никто не взял {humanize(now - task.updated_at)}",
                    task.updated_at)
    waiting_until = task.waiting_until
    if waiting_until is not None and waiting_until > now:
        return make(WAITING, f"ждёт до {waiting_until:%Y-%m-%d %H:%M} UTC "
                    f"(ещё {humanize(waiting_until - now)})", waiting_until)
    if task.lease_until is None or task.lease_until <= now:
        lease_end = task.lease_until or now
        return make(LEASE_EXPIRED, f"аренда истекла {humanize(now - lease_end)} назад",
                    lease_end)
    if waiting_until is not None:
        return make(STALE_PROGRESS,
                    f"срок ожидания вышел {humanize(now - waiting_until)} назад",
                    waiting_until)
    deadline = _stale_deadline(task, claimed_at)
    if deadline is not None and deadline <= now:
        last_update = task.last_progress_at or claimed_at or deadline
        return make(STALE_PROGRESS, f"нет обновления {humanize(now - last_update)}",
                    deadline)
    return make(IN_PROGRESS, "в работе", task.last_progress_at or claimed_at)


def attention_dict(a: Attention) -> dict[str, Any]:
    return {"state": a.state, "label": a.label_ru,
            "since": a.since.isoformat() if a.since else None,
            "last_result_at": a.last_result_at.isoformat() if a.last_result_at else None}

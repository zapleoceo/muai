"""Решения сторожа задач: чистая функция без доступа к БД, время приходит аргументом.

Правила (шаг 6):
- аренда истекла — одно событие `lease_expired` на пару (задача, fencing_token);
- аренда истекла и прогресса нет дольше `RECOVERY_FACTOR` длин аренды — задача
  возвращается в `open` (держатель снят, fencing_token прежний: записи старого
  держателя по-прежнему отвергаются); иначе задача остаётся как есть и видна в
  «Нужен я» через attention;
- контрольная точка просрочена и после неё нет прогресса — одно событие
  `watchdog_action` и одно сообщение исполнителю на каждую точку.
Пауза, вопрос владельцу, готово и отмена сторожем не трогаются.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vera_shared.room.attention import LEASE_EXPIRED, STALE_PROGRESS, Attention, humanize

RECOVERY_FACTOR = 2
MIN_LEASE = timedelta(seconds=900)  # нижняя граница «длины аренды»: короткая аренда не делает сторож злее


@dataclass(frozen=True)
class Action:
    kind: str  # вид события журнала: lease_expired | watchdog_action
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    reopen: bool = False
    notify: str | None = None  # кому слать сообщение в комнату


def lease_length(lease_until: datetime, claimed_at: datetime | None) -> timedelta:
    span = lease_until - claimed_at if claimed_at else MIN_LEASE
    return max(span, MIN_LEASE)


def decide(task: Any, att: Attention, *, now: datetime, claimed_at: datetime | None,
           last_event: tuple[str, int | None] | None,
           seen_checkpoints: frozenset[str]) -> list[Action]:
    """last_event — (вид, fencing_token) последнего события задачи; seen_checkpoints —
    метки контрольных точек, по которым сторож уже действовал."""
    if att.state == LEASE_EXPIRED and task.lease_until is not None:
        return _expired(task, att, now, claimed_at, last_event)
    if att.state == STALE_PROGRESS:
        return _checkpoint(task, now, seen_checkpoints)
    return []


def _expired(task: Any, att: Attention, now: datetime, claimed_at: datetime | None,
             last_event: tuple[str, int | None] | None) -> list[Action]:
    base = task.last_progress_at or claimed_at or task.lease_until
    idle = now - base
    limit = RECOVERY_FACTOR * lease_length(task.lease_until, claimed_at)
    reopen = idle >= limit
    out: list[Action] = []
    if last_event != ("lease_expired", task.fencing_token):
        out.append(Action("lease_expired", att.label_ru,
                          {"holder": task.lease_holder, "escalated": not reopen}))
    if reopen:
        out.append(Action(
            "watchdog_action",
            f"аренда {task.lease_holder} истекла, прогресса нет {humanize(idle)} "
            f"(порог {humanize(limit)}): задача возвращена в open",
            {"action": "reopen", "holder": task.lease_holder}, reopen=True))
    return out


def _checkpoint(task: Any, now: datetime, seen: frozenset[str]) -> list[Action]:
    cp, last = task.next_checkpoint_at, task.last_progress_at
    if cp is None or cp > now or (last is not None and last >= cp):
        return []
    mark = cp.isoformat()
    if mark in seen:
        return []
    who = task.lease_holder or (task.responsible or "")[:64] or None
    text = f"контрольная точка просрочена на {humanize(now - cp)}, прогресса после неё нет"
    return [Action("watchdog_action", text, {"action": "checkpoint", "checkpoint": mark},
                   notify=who)]

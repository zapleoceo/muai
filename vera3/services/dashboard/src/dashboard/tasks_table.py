"""Сводная таблица `/tasks`: ответственный, статус, последний реальный прогресс, итог, блокер, контрольная точка."""
from __future__ import annotations

from datetime import datetime

from vera_shared.owner_attention.policy import to_local
from vera_shared.room.attention import humanize

from dashboard.render import esc
from dashboard.tasks_service import TaskItem

HEAD = ("Ответственный", "Статус", "Последний прогресс", "Следующий результат",
        "Блокер", "Контрольная точка")
_BLOCKED = ("blocked", "waiting")


def _progress(item: TaskItem, now: datetime) -> str:
    at = item.row.last_progress_at
    return f"{humanize(now - at)} назад" if at else "—"


def _blocker(item: TaskItem) -> str:
    r = item.row
    return (r.waiting_reason or "—") if r.status in _BLOCKED else "—"


def _checkpoint(item: TaskItem) -> str:
    at = item.row.next_checkpoint_at
    return f"{to_local(at):%Y-%m-%d %H:%M}" if at else "—"


def tasks_table(items: list[TaskItem], now: datetime) -> str:
    if not items:
        return ""
    head = "".join(f"<th>{h}</th>" for h in HEAD)
    body = "".join(
        f'<tr><td>{esc(i.row.responsible or i.row.owner or "—")}</td>'
        f'<td>{esc(i.attention.label_ru)}</td><td>{esc(_progress(i, now))}</td>'
        f'<td>{esc(i.row.next_action or "—")}</td><td>{esc(_blocker(i))}</td>'
        f'<td>{esc(_checkpoint(i))}</td></tr>' for i in items)
    return (f'<table class="tk-table"><caption class="muted">Сводка (время Asia/Ho_Chi_Minh)</caption>'
            f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>')

"""Применить решения владельца об отмене, которые dot записал в Room, но не исполнил.

До кнопки владельца отменить задачу мог только её создатель, поэтому такие решения
висели на странице как «ожидает записи в Room». Теперь владелец исполняет их сам.
"""
from __future__ import annotations

import logging

from vera_shared.room.owner_task_actions import cancel_by_owner
from vera_shared.room.tasks import TaskBusy, TaskNotFound

from dashboard.office_hourly_service import load_report

log = logging.getLogger(__name__)


async def apply_pending_cancellations() -> dict[str, int]:
    report = await load_report()
    done = failed = 0
    for row, source in report.pending_cancellations or []:
        try:
            result = await cancel_by_owner(room=row.room, task_id=row.task_id,
                                           reason=f"Отмена владельца по решению {source}")
            done += bool(result.get("changed"))
        except (TaskBusy, TaskNotFound, ValueError) as e:
            failed += 1
            log.warning("owner cancel %s/%s refused: %s", row.room, row.task_id, type(e).__name__)
    return {"cancelled": done, "failed": failed}

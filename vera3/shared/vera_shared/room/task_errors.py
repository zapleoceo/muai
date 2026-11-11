"""Отказы операций над задачами комнаты."""
from __future__ import annotations


class TaskNotFound(LookupError):
    def __init__(self, room: str, task_id: str) -> None:
        super().__init__(f"task {task_id!r} not found in room {room!r}")


class TaskBusy(RuntimeError):
    """Задачу держит другой агент с живой арендой."""


class StaleLease(RuntimeError):
    """Аренда не твоя, истекла или fencing_token устарел."""

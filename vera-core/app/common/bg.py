"""Strong-reference background task registry.

asyncio.create_task() returns a future that the GC may collect if no
reference is kept. Use spawn() instead of bare create_task() for any
fire-and-forget coroutine that must survive until completion.
"""
import asyncio
import logging
from typing import Coroutine

log = logging.getLogger(__name__)

_TASKS: set[asyncio.Task] = set()


def spawn(coro: Coroutine, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_done)
    return task


def _done(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("background task %r failed: %r", task.get_name(), exc)


def count() -> int:
    return len(_TASKS)

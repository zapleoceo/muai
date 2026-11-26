"""Журнал вызовов хаба: запись фоновой задачей, чтобы не тормозить ответ агенту.

Пишутся только метаданные — агент, метод, имя инструмента, статус, длительность,
короткий вид ошибки. Ни аргументов, ни результатов, ни заголовков.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from vera_shared.hub.repo_agents import insert_call

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task[None]] = set()
Writer = Callable[[dict[str, Any]], Awaitable[None]]


async def _write(fields: dict[str, Any], writer: Writer) -> None:
    try:
        await writer(fields)
    except Exception as exc:  # журнал не должен ронять ответ агенту
        log.warning("hub: call log insert failed: %s", type(exc).__name__)


def record_call(agent: str, method: str, status: str, duration_ms: int, *, tool: str = "",
                project: str = "", upstream: str = "", error: str = "",
                writer: Writer | None = None) -> None:
    fields = {"agent": agent[:32], "method": method[:32], "tool": tool[:160],
              "project": project[:16], "upstream": upstream[:16], "status": status[:16],
              "duration_ms": duration_ms, "error": error[:120]}
    task = asyncio.create_task(_write(fields, writer or insert_call))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain_calls() -> None:
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)

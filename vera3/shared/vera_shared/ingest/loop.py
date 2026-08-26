"""Каркас цикла опроса источника.

Здесь зашито одно правило, которое легко забыть в собственном цикле: при
отсутствии или отзыве ключа контейнер НЕ падает. С `restart: unless-stopped`
падение означает crash-loop — так и было с ingestor-instagram, пока сессия
неактивна (обычное состояние между логинами), RestartCount рос без предела.
Вместо падения — долгая пауза и новая попытка.

Всё остальное — дело источника: `poll_once` держит своего клиента и курсоры,
цикл только зовёт его и не даёт умереть.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

#: пауза после отказа в доступе. Долгая сознательно: ключ появляется руками.
AUTH_RETRY_S = 600.0


async def poll_forever(
    *,
    name: str,
    poll_once: Callable[[], Awaitable[None]],
    interval_s: float,
    auth_error: type[Exception] | tuple[type[Exception], ...],
    auth_retry_s: float = AUTH_RETRY_S,
    log: logging.Logger | None = None,
) -> None:
    """Опрашивать источник вечно. Не возвращается; отменяется только снаружи."""
    log = log or logging.getLogger(name)
    while True:
        try:
            await poll_once()
        except asyncio.CancelledError:
            raise
        except auth_error as e:
            log.error("%s: нет доступа (%s) — жду ключ %.0f мин",
                      name, e, auth_retry_s / 60)
            await asyncio.sleep(auth_retry_s)
            continue
        except Exception as e:  # noqa: BLE001 — цикл обязан переживать любой сбой прогона
            log.exception("%s: сбой прогона: %s", name, e)
        await asyncio.sleep(interval_s)

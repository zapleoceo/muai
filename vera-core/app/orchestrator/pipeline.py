import logging
import time
from typing import Awaitable, Callable

from app.db.tasks import save_task, update_task
from app.orchestrator.loop import run_agentic
from app.orchestrator.memory import add_turn

log = logging.getLogger(__name__)

ProgressCb = Callable[[str], Awaitable[None]]


async def _noop(_: str) -> None:
    pass


async def run(input_text: str, user_id: int | None,
              progress_cb: ProgressCb | None = None) -> str:
    cb = progress_cb or _noop
    started = time.monotonic()
    task_id = await save_task("telegram", user_id, input_text)

    try:
        reply = await run_agentic(input_text, user_id, cb)
    except Exception as exc:
        log.exception("Agentic loop crashed: %s", exc)
        reply = f"⚠️ Сбой: {exc}"

    duration_ms = int((time.monotonic() - started) * 1000)
    await update_task(task_id, reply, 1.0, 1, [], duration_ms)
    add_turn(user_id, input_text, reply)
    return reply or "Готово."

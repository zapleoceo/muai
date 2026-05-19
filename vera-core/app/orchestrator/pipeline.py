import logging
import time
from typing import Awaitable, Callable

from app.db.tasks import save_task, update_task
from app.orchestrator.dispatcher import dispatch
from app.orchestrator.memory import add_turn
from app.orchestrator.prefilter import prefilter
from app.orchestrator.responder import respond

log = logging.getLogger(__name__)

ProgressCb = Callable[[str], Awaitable[None]]


async def _noop(_: str) -> None:
    pass


async def run(input_text: str, user_id: int | None,
              progress_cb: ProgressCb | None = None) -> str:
    cb = progress_cb or _noop
    started = time.monotonic()
    task_id = await save_task("telegram", user_id, input_text)

    await cb("🧭 Разбираю запрос...")
    intent = await prefilter(input_text, user_id=user_id)

    if not intent.target_agents:
        await cb("💭 Думаю...")
        final = await _self_answer(input_text)
        score, attempts = 1.0, 1
    else:
        agents = ", ".join(intent.target_agents)
        await cb(f"🔎 Отправляю задачу: {agents}")
        final, score, attempts = await _orchestrate(intent, input_text, task_id, cb)

    duration_ms = int((time.monotonic() - started) * 1000)
    await update_task(
        task_id, final, score, attempts, list(intent.target_agents), duration_ms
    )
    reply = final or "Готово."
    add_turn(user_id, input_text, reply)
    return reply


async def _orchestrate(intent, request: str, task_id: int,
                       cb: ProgressCb) -> tuple[str, float, int]:
    intent_ctx = dict(intent.context)

    await cb("📡 Жду ответ от агентов...")
    results = await dispatch(intent.target_agents, request, intent_ctx, task_id)

    if not results:
        return await _self_answer(request), 1.0, 1

    if all(not r.success for r in results):
        msgs = "; ".join(f"{r.agent_id}: {r.summary or r.error}" for r in results)
        return f"Не удалось выполнить: {msgs}", 0.0, 1

    await cb("📝 Формирую ответ...")
    reply = await respond(request, results)
    return reply, 1.0, 1


async def _self_answer(text: str) -> str:
    try:
        from vera_shared.providers.registry import get_registry
        out, _, _ = await get_registry().chat(
            "chat:fast", [{"role": "user", "content": text}]
        )
        return out
    except Exception as exc:
        log.warning("Self-answer failed: %s", exc)
        return "Сервис временно недоступен (нет свободных AI-токенов). Попробуй через минуту."

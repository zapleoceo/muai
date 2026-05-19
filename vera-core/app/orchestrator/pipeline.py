import logging
import time

from app.db.tasks import save_task, update_task
from app.orchestrator.dispatcher import dispatch
from app.orchestrator.evaluator import evaluate
from app.orchestrator.prefilter import prefilter
from app.orchestrator.responder import respond

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_QUALITY_THRESHOLD = 0.6


async def run(input_text: str, user_id: int | None) -> str:
    started = time.monotonic()
    task_id = await save_task("telegram", user_id, input_text)
    intent = await prefilter(input_text)

    if not intent.target_agents:
        final = await _self_answer(input_text)
        score, attempts = 1.0, 1
    else:
        final, score, attempts = await _orchestrate(intent, input_text, task_id)

    duration_ms = int((time.monotonic() - started) * 1000)
    await update_task(
        task_id, final, score, attempts, list(intent.target_agents), duration_ms
    )
    return final or "Готово."


async def _orchestrate(intent, request: str, task_id: int) -> tuple[str, float, int]:
    last_reply = ""
    score = 0.0
    intent_ctx = dict(intent.context)

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        results = await dispatch(intent.target_agents, request, intent_ctx, task_id)

        if not results:
            return await _self_answer(request), 1.0, attempt

        reply = await respond(request, results)
        score = await evaluate(request, reply)
        log.info("Attempt %d score=%.2f reply_len=%d", attempt, score, len(reply))
        last_reply = reply

        if score >= _QUALITY_THRESHOLD:
            return reply, score, attempt

    return last_reply, score, _MAX_ATTEMPTS


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

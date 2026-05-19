import logging
import time

from app.db.tasks import save_task, update_task
from app.orchestrator.dispatcher import dispatch
from app.orchestrator.evaluator import evaluate
from app.orchestrator.prefilter import prefilter
from app.orchestrator.prompt_builder import build_prompts
from app.orchestrator.retry import optimize_prompt

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_QUALITY_THRESHOLD = 0.7


async def run(input_text: str, user_id: int | None) -> str:
    started = time.monotonic()
    task_id = await save_task("telegram", user_id, input_text)
    intent = await prefilter(input_text)

    if not intent.target_agents:
        final = await _self_answer(input_text)
        score, attempts = 1.0, 1
    else:
        final, score, attempts = await _orchestrate(intent, input_text)

    duration_ms = int((time.monotonic() - started) * 1000)
    await update_task(task_id, final, score, attempts, list(intent.target_agents), duration_ms)
    return final or "Не удалось получить ответ."


async def _orchestrate(intent, input_text: str) -> tuple[str, float, int]:
    current_text = input_text
    final = ""
    score = 0.0
    attempt = 1

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        prompts = build_prompts(intent, current_text, attempt)
        results = await dispatch(intent.target_agents, prompts)
        if not results:
            return await _self_answer(input_text), 1.0, attempt

        score, final = await evaluate(input_text, results)
        log.info("Attempt %d score=%.2f", attempt, score)

        if score >= _QUALITY_THRESHOLD:
            break
        if attempt < _MAX_ATTEMPTS:
            current_text = await optimize_prompt(current_text, results, attempt)

    return final, score, attempt


async def _self_answer(text: str) -> str:
    try:
        from vera_shared.providers.registry import get_registry
        text_out, _, _ = await get_registry().chat("chat:fast", [{"role": "user", "content": text}])
        return text_out
    except Exception as exc:
        log.warning("Self-answer failed: %s", exc)
        return "Сервис временно недоступен (нет свободных AI-токенов). Попробуй через минуту."

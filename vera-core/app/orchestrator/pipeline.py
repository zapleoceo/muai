import logging
import time

from app.db.tasks import save_task, update_task
from app.orchestrator.dispatcher import dispatch
from app.orchestrator.evaluator import evaluate
from app.orchestrator.prefilter import prefilter
from app.orchestrator.prompt_builder import build_prompts
from app.orchestrator.retry import optimize_prompt

log = logging.getLogger(__name__)


async def run(input_text: str, user_id: int | None) -> str:
    started = time.monotonic()
    task_id = await save_task("telegram", user_id, input_text)
    intent = await prefilter(input_text)

    current_text = input_text
    final = ""
    score = 0.0

    for attempt in range(1, 4):
        prompts = build_prompts(intent, current_text, attempt)
        results = await dispatch(intent.target_agents, prompts)

        if not results:
            results = {"vera-core-fallback": await _self_answer(current_text)}

        score, final = await evaluate(input_text, results)
        log.info("Attempt %d score=%.2f", attempt, score)

        if score >= 0.7:
            break

        if attempt < 3:
            current_text = await optimize_prompt(current_text, results, attempt)

    duration_ms = int((time.monotonic() - started) * 1000)
    await update_task(task_id, final, score, attempt, list(intent.target_agents), duration_ms)
    return final


async def _self_answer(text: str) -> str:
    try:
        from vera_shared.providers.registry import get_registry
        text_out, _, _ = await get_registry().chat("chat:fast", [{"role": "user", "content": text}])
        return text_out
    except Exception as exc:
        log.warning("Self-answer failed: %s", exc)
        return f"Не удалось получить ответ: {exc}"

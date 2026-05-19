import logging
import re

log = logging.getLogger(__name__)

_JUDGE_SYSTEM = (
    "You are a quality judge for AI responses. "
    "Rate the response on completeness and accuracy from 0 to 10. "
    "Return only a JSON object: {\"score\": <int 0-10>, \"answer\": \"<best combined answer>\"}"
)


def _combine(results: dict[str, str]) -> str:
    parts = [v for v in results.values() if not v.startswith("ERROR:")]
    return "\n\n".join(parts) if parts else next(iter(results.values()), "")


async def evaluate(original_request: str, results: dict[str, str]) -> tuple[float, str]:
    combined = _combine(results)

    if not combined or combined.startswith("ERROR:"):
        return 0.0, combined

    valid = [v for v in results.values() if not v.startswith("ERROR:")]
    if len(valid) == 1:
        return 0.8, valid[0]

    prompt = (
        f"User request: {original_request}\n\n"
        f"Agent responses:\n{combined}\n\n"
        "Rate and combine the best answer."
    )

    try:
        from vera_shared.providers.registry import get_registry
        import json
        registry = get_registry()
        raw, _, _ = await registry.chat("chat:fast", [{"role": "user", "content": prompt}], system=_JUDGE_SYSTEM)
        data = json.loads(raw)
        score = float(data.get("score", 5)) / 10.0
        answer = data.get("answer", combined)
        return min(max(score, 0.0), 1.0), answer
    except Exception as exc:
        log.warning("Evaluator failed: %s", exc)
        return 0.5, combined

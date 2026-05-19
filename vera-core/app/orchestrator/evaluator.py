import json
import logging
import re

log = logging.getLogger(__name__)

_SYSTEM = (
    "Ты — судья качества ответов AI-ассистента. "
    "Тебе дан запрос пользователя и ответ ассистента. "
    "Оцени КАЧЕСТВО ответа от 0 до 10 по критериям: "
    "точность, полнота, релевантность запросу, читабельность. "
    "Верни строго JSON: {\"score\": <0-10>, \"reason\": \"<кратко>\"}"
)


async def evaluate(request: str, reply: str) -> float:
    if not reply or reply.startswith("Сервис временно недоступен"):
        return 0.0

    prompt = (
        f"Запрос пользователя:\n{request}\n\n"
        f"Ответ ассистента:\n{reply}\n\n"
        "Оцени."
    )

    try:
        from vera_shared.providers.registry import get_registry
        raw, _, _ = await get_registry().chat(
            "chat:fast", [{"role": "user", "content": prompt}], system=_SYSTEM
        )
        score = _extract_score(raw)
        return max(0.0, min(1.0, score / 10.0))
    except Exception as exc:
        log.warning("Evaluator failed, defaulting to 0.6: %s", exc)
        return 0.6


def _extract_score(raw: str) -> float:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return float(json.loads(raw).get("score", 5))
    except Exception:
        m = re.search(r'"?score"?\s*[:=]\s*(\d+(?:\.\d+)?)', raw)
        return float(m.group(1)) if m else 5.0

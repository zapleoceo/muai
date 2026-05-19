import json
import logging

from app.orchestrator.dispatcher import AgentResult

log = logging.getLogger(__name__)

_SYSTEM = (
    "Ты — Vera, AI-оркестратор. Тебе пришли результаты работы агентов "
    "(инструментов). Сформируй краткий, осмысленный ответ пользователю "
    "на его исходный запрос на ОСНОВЕ этих данных. "
    "Пиши по-русски, лаконично, по делу. Не дублируй сырой JSON — "
    "перескажи суть. Если данных мало или ошибка — честно скажи об этом."
)

_MAX_DATA_CHARS = 12000


def _trim(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if len(s) <= _MAX_DATA_CHARS:
        return s
    return s[:_MAX_DATA_CHARS] + f"\n… (обрезано, всего {len(s)} символов)"


async def respond(request: str, results: list[AgentResult]) -> str:
    errors = [r for r in results if not r.success]
    successes = [r for r in results if r.success]

    if not successes:
        msgs = "; ".join(f"{r.agent_id}: {r.summary or r.error}" for r in errors)
        return f"Не удалось выполнить: {msgs}"

    blocks: list[str] = []
    for r in successes:
        blocks.append(f"### {r.agent_id} — {r.summary}\n{_trim(r.data)}")
    payload = "\n\n".join(blocks)

    prompt = (
        f"Исходный запрос пользователя:\n{request}\n\n"
        f"Результаты агентов:\n{payload}\n\n"
        "Составь ответ пользователю."
    )

    try:
        from vera_shared.providers.registry import get_registry
        text, _, _ = await get_registry().chat(
            "chat:fast",
            [{"role": "user", "content": prompt}],
            system=_SYSTEM,
        )
        return text.strip() or successes[0].summary
    except Exception as exc:
        log.warning("Responder fallback: %s", exc)
        # Plain text fallback if LLM is down
        lines = [r.summary for r in successes if r.summary]
        return "\n".join(lines) or "Готово."

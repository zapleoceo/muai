"""Free-text follow-up to a triage card: LLM picks tool, executes."""
import json
import logging
import re

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

from app.orchestrator.tool_router import call_tool, collect_tools, format_tools_for_prompt

log = logging.getLogger(__name__)

_SYSTEM = """Ты — Vera. Дима выбрал «Свой ответ» по карточке события и написал
тебе инструкцию текстом. Ты получаешь:
- описание события (источник, отправитель, текст, метаданные)
- список доступных TOOLS
- свободную инструкцию Димы

Твоя задача: понять что Дима хочет, выбрать ОДИН инструмент из TOOLS и
сформировать args.

КРИТИЧНО:
- ВСЕ значения args бери ТОЛЬКО из event.account / event.metadata / event.entity_hints.
- НИКОГДА не выдумывай email, thread_id, peer, ids. Если нужного поля
  нет в event — верни {"tool": null, "reply": "не могу — нет <поле>"}.
- Для Gmail: email = event.account, thread_id = event.metadata.thread_id.
- Если по инструкции инструмент не нужен ("забей", "ничего"),
  верни {"tool": null, "reply": "короткое подтверждение"}.

Верни ТОЛЬКО JSON, без markdown:
{"tool": "имя_или_null", "args": {...}, "reply": "что сказать Диме"}"""


from vera_shared.llm.json_parse import strip_fence as _strip_fence  # noqa: F401


async def handle(event_id: int, instruction: str) -> str:
    async with get_session() as session:
        event = await session.get(Event, event_id)
    if event is None:
        return "⚠️ Событие не найдено."

    specs, route = await collect_tools()
    event_block = {
        "source": event.source,
        "category": event.category,
        "account": event.account,
        "content_text": (event.content_text or "")[:1500],
        "metadata": event.metadata_ or {},
        "entity_hints": event.entity_hints or [],
    }
    prompt = (
        f"EVENT:\n{json.dumps(event_block, ensure_ascii=False, default=str)}\n\n"
        f"TOOLS:\n{format_tools_for_prompt(specs)}\n\n"
        f"ИНСТРУКЦИЯ ДИМЫ:\n{instruction}"
    )

    from vera_shared.llm import chat
    try:
        raw = await chat([{"role": "user", "content": prompt}],
                         system=_SYSTEM, capability="chat:smart")
    except Exception as exc:
        log.warning("followup LLM failed: %s", exc)
        return f"⚠️ LLM ошибка: {exc}"

    try:
        data = json.loads(_strip_fence(raw))
    except Exception:
        return f"⚠️ Не смогла распарсить ответ LLM: {raw[:200]}"

    tool = data.get("tool")
    reply = data.get("reply") or ""
    if not tool:
        return reply or "Ок, ничего не делаю."

    result = await call_tool(route, tool, data.get("args") or {})
    from app.triage.dispatcher import save_execution
    await save_execution(event_id, tool, data.get("args") or {}, result)
    mark = "✅" if result.get("ok") else "⚠️"
    preview = str(result.get("result") or result.get("error") or "")[:300]
    return f"{mark} {tool}\n{reply}\n<code>{preview}</code>"

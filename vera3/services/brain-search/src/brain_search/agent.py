"""Vera as a ReAct-style agent loop.

Provider-agnostic (we don't use OpenAI native tool_use because not every
provider in our pool supports it identically). Each step the LLM must
emit STRICT JSON:

  {"thought": "...", "action": "tool", "name": "...", "params": {...}}
or
  {"thought": "...", "action": "answer", "text": "..."}

Loop until 'answer' or max_steps. Telemetry logged per step.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from vera_shared.llm.client import LLMCallFailed, chat_async

from brain_search.agent_tools import (
    BUILTIN_SPECS,
    TELEGRAM_TOOLS_URL,
    ToolDescriptor,
    execute_tool,
    load_remote_tool_specs,
)
from brain_search.evidence import EVIDENCE_RULES, evidence_excerpt

log = logging.getLogger(__name__)

# Потолок на ОДИН шаг агента — иначе зависший брокер-вызов вешает /search навсегда.
AGENT_STEP_TIMEOUT_S = float(os.environ.get("AGENT_STEP_TIMEOUT_S", "90"))

@dataclass
class AgentTrace:
    steps: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    final_step: int = 0
    cost_usd: float = 0.0
    provider_last: str | None = None


# ─── Loop ─────────────────────────────────────────────────────────────────────


async def collect_tools() -> list[ToolDescriptor]:
    tools = list(BUILTIN_SPECS)
    tools.extend(await load_remote_tool_specs(TELEGRAM_TOOLS_URL))
    return tools


def _render_tools(tools: list[ToolDescriptor]) -> str:
    out = []
    for t in tools:
        out.append(f"### {t.name}\n{t.description}\nparams_schema: "
                   f"{json.dumps(t.params_schema, ensure_ascii=False)}")
    return "\n\n".join(out)


SYSTEM_PROMPT = """Ты — Вера, цифровая память Димы. Ты работаешь в режиме AGENT LOOP:
на КАЖДОМ шаге ты возвращаешь СТРОГО ОДИН JSON-объект — либо вызов инструмента,
либо финальный ответ.

Формат вызова инструмента:
{"thought": "почему я хочу это позвать", "action": "tool", "name": "<tool>", "params": {...}}

Формат ответа Диме:
{"thought": "итог рассуждения", "action": "answer", "text": "<полный ответ на русском>"}

Правила:
1. Не повторяй один и тот же tool с теми же параметрами.
2. Если данных в начальном контексте уже достаточно — отвечай сразу, без tool.
3. Если вопрос про подключённые источники / тебя саму — отвечай по конфигурации, не ищи в событиях.
4. Когда вычислила нетривиальный факт (число участников, имена соучредителей и т.п.) — позови
   memory.remember чтобы запомнить, и ТОЛЬКО ПОТОМ отвечай.
5. Каждый ответ цитируй фактами — числами и именами, не общими словами.
6. Максимум 6 шагов. Если не справилась — отвечай честно тем что есть.
7. События source=perplexity — это ЗАПРОСЫ Димы к Perplexity AI (намерения,
   вопросы), а НЕ выполненная работа. НИКОГДА не описывай их как
   «сделано/выполнено». source=vera_chat — прошлые разговоры с тобой, не факты.
   Реальная работа дня живёт в source=gmail (письма) и source=telegram (чаты).
8. Если вопрос содержит период («вчера», «за неделю», дату) — у search_events
   есть параметры date_from/date_to (ISO). Используй их, не полагайся на
   текстовое совпадение слова «вчера».
9. АВТОРСТВО — критично. У каждого события в search_events есть поля
   author_role и author_label. author_role='self' значит писал ДИМА.
   author_role='counterparty' значит писал собеседник. chat_title — это
   название чата, НЕ автор сообщения; в личке chat_title = имя собеседника,
   но сообщение может быть и моё (self), и его (counterparty). НИКОГДА не
   определяй автора по chat_title. Когда цитируешь — пиши «Я →» для self,
   «<author_label> →» для counterparty.
10. Для каждого существенного факта в ответе указывай [event:ID] из
    найденных событий. Если у события есть source_url, добавь ссылку.
    Не придумывай адрес оригинала, если source_url отсутствует.
"""
SYSTEM_PROMPT += EVIDENCE_RULES


def _observation_text(name: str, obs: Any) -> str:
    """Send complete, attributable search cards within the observation budget."""
    if name == "search_events" and isinstance(obs, dict) and isinstance(obs.get("events"), list):
        events = obs["events"]
        selected: list[dict[str, Any]] = []
        for event in events[:3]:
            if not isinstance(event, dict):
                continue
            # Only these fields are needed to attribute the quoted excerpt.
            # Bound each field before serializing; never cut serialized JSON.
            card = {
                "event_id": event.get("event_id"),
                "source": str(event.get("source") or "")[:40],
                "preview": evidence_excerpt(event.get("preview"), 650),
                "occurred_at": str(event.get("occurred_at") or "")[:32],
                "author_role": str(event.get("author_role") or "")[:30],
                "author_label": str(event.get("author_label") or "")[:120],
                "chat_title": str(event.get("chat_title") or "")[:120],
            }
            url = event.get("source_url")
            card["source_url"] = url if isinstance(url, str) and len(url) <= 300 else None
            candidate = {"found": obs.get("found"), "events": selected + [card],
                         "omitted_events": len(events) - len(selected) - 1}
            if len(json.dumps(candidate, ensure_ascii=False)) > 3000:
                break
            selected.append(card)
        obs = {"found": obs.get("found"), "events": selected,
               "omitted_events": len(events) - len(selected)}
        return json.dumps(obs, ensure_ascii=False)
    rendered = json.dumps(obs, ensure_ascii=False)
    return evidence_excerpt(rendered, 3000)


async def run_agent(
    *,
    user_query: str,
    initial_context: str,
    self_context: str,
    history_block: str,
    max_steps: int = 6,
) -> AgentTrace:
    tools = await collect_tools()
    tools_block = _render_tools(tools)
    tools_by_name = {t.name: t for t in tools}

    trace = AgentTrace()
    transcript: list[dict[str, str]] = []

    base_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"### Твоя конфигурация:\n{self_context}\n\n"
        f"{history_block}"
        f"### Доступные инструменты:\n{tools_block}\n\n"
        f"### Начальный контекст из БД событий (top-K FTS):\n{initial_context}\n\n"
        f"### Вопрос Димы:\n{user_query}\n\n"
        f"Верни СТРОГО JSON для текущего шага. Только JSON, без префиксов."
    )

    for step in range(1, max_steps + 1):
        step_prompt = base_prompt
        if transcript:
            step_prompt += "\n\n### История твоих шагов и наблюдений:\n"
            for t in transcript:
                step_prompt += f"\n{t['role']}: {t['content']}\n"
            step_prompt += (
                "\nСледующий шаг — снова СТРОГО ОДИН JSON. Если уже знаешь "
                "ответ — action='answer'."
            )

        try:
            raw, meta = await asyncio.wait_for(chat_async(
                messages=[{"role": "user", "content": step_prompt}],
                capability="chat:smart",
                response_format={"type": "json_object"},
                max_tokens=1200,
                temperature=0.2,
                workflow="agent_loop",
            ), timeout=AGENT_STEP_TIMEOUT_S)
            trace.provider_last = meta.get("provider")
            trace.cost_usd += float(meta.get("cost_usd") or 0)
        except asyncio.TimeoutError:
            trace.answer = "LLM не ответил вовремя (агентный шаг превысил таймаут)."
            return trace
        except LLMCallFailed as e:
            trace.answer = f"LLM недоступен ({e})."
            return trace

        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                trace.steps.append({"step": step, "raw": raw, "error": "no JSON"})
                continue
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                trace.steps.append({"step": step, "raw": raw, "error": "invalid JSON"})
                continue

        if not isinstance(parsed, dict):
            trace.steps.append({"step": step, "raw": raw, "error": "not an object"})
            continue

        action = parsed.get("action")
        if action == "answer":
            trace.answer = parsed.get("text", "").strip() or "(пусто)"
            trace.final_step = step
            trace.steps.append({"step": step, **parsed})
            return trace

        if action == "tool":
            name = parsed.get("name", "")
            params = parsed.get("params", {}) or {}
            tool = tools_by_name.get(name)
            if tool is None:
                obs = {"error": f"unknown tool: {name}",
                       "available": list(tools_by_name.keys())}
            else:
                obs = await execute_tool(tool, params)

            trace.steps.append({"step": step, **parsed, "observation": obs})
            transcript.append({"role": "assistant",
                                "content": json.dumps(parsed, ensure_ascii=False)[:2000]})
            transcript.append({"role": "tool",
                                "content": f"{name} → {_observation_text(name, obs)}"})
            continue

        # Unknown action — record and continue
        trace.steps.append({"step": step, "raw": parsed, "error": "no action"})
        transcript.append({"role": "assistant",
                            "content": json.dumps(parsed, ensure_ascii=False)[:1000]})

    # Out of steps
    if not trace.answer:
        trace.answer = (
            "Я попробовала несколько подходов, но не пришла к точному ответу за "
            f"{max_steps} шагов. Попробуй уточнить вопрос."
        )
    return trace

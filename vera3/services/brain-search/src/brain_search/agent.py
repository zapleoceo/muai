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

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from vera_shared.llm.client import LLMCallFailed, chat

log = logging.getLogger(__name__)

TELEGRAM_TOOLS_URL = os.environ.get(
    "TELEGRAM_TOOLS_URL", "http://ingestor-telegram:8000"
)
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


# ─── Tool plumbing ────────────────────────────────────────────────────────────


@dataclass
class ToolDescriptor:
    name: str
    description: str
    params_schema: dict[str, Any]
    invoker: str  # 'http:telegram' | 'builtin:search_events' | 'builtin:memory'


@dataclass
class AgentTrace:
    steps: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    final_step: int = 0
    cost_usd: float = 0.0
    provider_last: str | None = None


async def _load_remote_tool_specs(url: str) -> list[ToolDescriptor]:
    """Fetch /tools/spec from a remote ingestor and adapt."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{url}/tools/spec")
        if r.status_code >= 400:
            log.warning("remote /tools/spec returned %s", r.status_code)
            return []
        specs = r.json()
    except Exception as e:
        log.warning("failed to fetch %s/tools/spec: %s", url, e)
        return []
    return [
        ToolDescriptor(
            name=s["name"],
            description=s["description"],
            params_schema=s["params_schema"],
            invoker="http:telegram",
        )
        for s in specs
    ]


async def _exec_http_telegram(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    short = tool_name.split(".", 1)[-1]
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{TELEGRAM_TOOLS_URL}/tools/{short}",
                json=params,
                headers={"X-Internal-Secret": INTERNAL_SECRET},
            )
        if r.status_code >= 400:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:300]}
        return r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ─── Built-in tools ───────────────────────────────────────────────────────────


BUILTIN_SPECS: list[ToolDescriptor] = [
    ToolDescriptor(
        name="search_events",
        description=(
            "Full-text search across ALL events (telegram, gmail, instagram, "
            "vera_chat). Use when you need MORE messages than initial context "
            "already shows. For time-bound questions (вчера, за неделю, дата) "
            "ALWAYS pass date_from/date_to (ISO date, e.g. 2026-06-09) — "
            "an empty q with dates returns everything in the period."
        ),
        params_schema={
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "source": {"type": "string",
                            "enum": ["telegram", "gmail", "instagram", "vera_chat", "any"]},
                "limit": {"type": "integer", "default": 20},
                "date_from": {"type": "string",
                               "description": "ISO date inclusive, e.g. 2026-06-09"},
                "date_to": {"type": "string",
                             "description": "ISO date inclusive, e.g. 2026-06-09"},
            },
            "required": ["q"],
        },
        invoker="builtin:search_events",
    ),
    ToolDescriptor(
        name="memory.remember",
        description=(
            "Save a long-lived fact into Vera's own brain (source='vera_memory'). "
            "Use this AFTER deriving a non-obvious truth from tool calls, so future "
            "questions don't repeat the same work. Example: after counting members "
            "of group X, remember the count + date."
        ),
        params_schema={
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "Plain Russian sentence."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["fact"],
        },
        invoker="builtin:memory",
    ),
]


def _parse_iso_date(raw: str | None) -> "datetime | None":
    from datetime import datetime as _dt
    if not raw:
        return None
    try:
        return _dt.strptime(raw.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


async def _exec_search_events(q: str, source: str = "any",
                                limit: int = 20,
                                date_from: str | None = None,
                                date_to: str | None = None) -> dict[str, Any]:
    from datetime import timedelta
    from sqlalchemy import text
    from vera_shared.db.engine import get_session
    from brain_search.query_parse import TZ_OFFSET_H

    STOPWORDS = {"что", "как", "и", "в", "на", "о", "по", "у", "для", "это",
                 "что-то", "ли", "ну", "же", "то", "был", "была", "были",
                 "быть", "есть", "не", "ни", "при", "из", "за",
                 "ты", "я", "мне", "мы", "вы", "он", "она", "они"}
    raw_words = re.findall(r"[\wа-яА-ЯёЁ]+", q)
    words = [w for w in raw_words if len(w) >= 2 and w.lower() not in STOPWORDS]
    ts_query = " | ".join(f"{w}:*" for w in words) if words else ""

    params: dict[str, Any] = {"tsq": ts_query, "lim": limit}
    where_extra = ""
    if source != "any":
        where_extra += " AND source = :src"
        params["src"] = source

    # Даты — локальные (Jakarta), храним naive UTC → сдвиг на -TZ_OFFSET_H.
    d_from = _parse_iso_date(date_from)
    d_to = _parse_iso_date(date_to)
    if d_from:
        params["d_from"] = d_from - timedelta(hours=TZ_OFFSET_H)
        where_extra += " AND occurred_at >= :d_from"
    if d_to:
        # inclusive конец дня
        params["d_to"] = d_to + timedelta(days=1) - timedelta(hours=TZ_OFFSET_H)
        where_extra += " AND occurred_at < :d_to"

    async with get_session() as s:
        if ts_query:
            stmt = text(f"""
                SELECT id, source, occurred_at, content_text
                FROM events
                WHERE to_tsvector('russian', content_text)
                      @@ to_tsquery('russian', :tsq) {where_extra}
                ORDER BY ts_rank(to_tsvector('russian', content_text),
                                  to_tsquery('russian', :tsq)) DESC,
                         occurred_at DESC
                LIMIT :lim
            """)
        else:
            stmt = text(f"""
                SELECT id, source, occurred_at, content_text
                FROM events
                WHERE 1=1 {where_extra}
                ORDER BY occurred_at DESC LIMIT :lim
            """)
        rs = (await s.execute(stmt, params)).all()
    return {
        "found": len(rs),
        "events": [
            {"event_id": r[0], "source": r[1],
             "occurred_at": str(r[2])[:19],
             "preview": (r[3] or "")[:400]}
            for r in rs
        ],
    }


async def _exec_memory_remember(fact: str, tags: list[str] | None = None,
                                  confidence: float = 0.8) -> dict[str, Any]:
    from datetime import datetime
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    now = datetime.utcnow()
    async with get_session() as s:
        ev = EventRow(
            source="vera_memory",
            source_event_id=f"memory:{now.timestamp()}",
            account="vera",
            category="fact",
            content_text=fact[:8000],
            occurred_at=now,
            metadata_={"tags": tags or [], "confidence": confidence},
            triage_status="pending",
        )
        s.add(ev)
        await s.flush()
        return {"saved": True, "event_id": ev.id}


# ─── Loop ─────────────────────────────────────────────────────────────────────


async def collect_tools() -> list[ToolDescriptor]:
    tools = list(BUILTIN_SPECS)
    tools.extend(await _load_remote_tool_specs(TELEGRAM_TOOLS_URL))
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
"""


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
            raw, meta = await chat(
                messages=[{"role": "user", "content": step_prompt}],
                capability="chat:smart",
                response_format={"type": "json_object"},
                max_tokens=1200,
                temperature=0.2,
                workflow="agent_loop",
            )
            trace.provider_last = meta.get("provider")
            trace.cost_usd += float(meta.get("cost_usd") or 0)
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
                if tool.invoker == "http:telegram":
                    obs = await _exec_http_telegram(name, params)
                elif tool.invoker == "builtin:search_events":
                    obs = await _exec_search_events(**params)
                elif tool.invoker == "builtin:memory":
                    obs = await _exec_memory_remember(**params)
                else:
                    obs = {"error": f"no invoker for {tool.invoker}"}

            trace.steps.append({"step": step, **parsed, "observation": obs})
            transcript.append({"role": "assistant",
                                "content": json.dumps(parsed, ensure_ascii=False)[:2000]})
            transcript.append({"role": "tool",
                                "content": f"{name} → {json.dumps(obs, ensure_ascii=False)[:3000]}"})
            continue

        # Unknown action — record and continue
        trace.steps.append({"step": step, "raw": parsed, "error": "no action"})
        transcript.append({"role": "assistant",
                            "content": json.dumps(parsed, ensure_ascii=False)[:1000]})

    # Out of steps
    if not trace.answer:
        trace.answer = (
            "Я попробовала несколько подходов, но не пришла к точному ответу за "
            f"{max_steps} шагов. Попробуй спросить уже́е."
        )
    return trace

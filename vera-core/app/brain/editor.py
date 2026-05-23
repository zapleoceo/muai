"""Brain editor: parse Dima's free-text instructions into graph operations.

When Dima writes "всегда отвечай маме в течение часа" or "никогда не
отправляй деньги без подтверждения", this module asks an LLM to map
the sentence to one of:

  - new_goal(title, metric, deadline, about)
  - new_value(statement, tool_pattern)
  - new_nogo(description, tool_pattern, targets)
  - new_style(relationship_id, tone, examples)
  - deactivate(label, id)

The LLM call is small (single turn, JSON output), routed through the
existing litellm router so it inherits the gemini→deepseek→anthropic
fallback chain.
"""
from __future__ import annotations

import json
import logging

from vera_shared.llm.router import chat as llm_chat

from app.brain import identity as ID

log = logging.getLogger(__name__)


_SYSTEM = """Ты — парсер текстовых инструкций Димы для Веры. Дима пишет тебе
русским языком пожелание, правило, цель или запрет. Твоя задача —
выдать одну операцию над графом в JSON.

Возможные операции:
  {"op":"new_goal", "title":"...", "metric":"...", "deadline":"YYYY-MM-DD", "about":["entity_id",...]}
  {"op":"new_value", "statement":"...", "tool_pattern":"regex|null"}
  {"op":"new_nogo", "description":"...", "tool_pattern":"regex", "targets":["entity_id",...]}
  {"op":"new_style", "relationship_id":"...", "tone":"...", "examples":["...","..."]}
  {"op":"deactivate", "label":"Goal|Value|NoGo|Style", "id":"..."}
  {"op":"none", "reason":"..."}    // если ничего не подходит

Отвечай только валидным JSON, без пояснений. Если не уверен — op:none."""


async def parse_and_apply(text: str) -> dict:
    """Send text to LLM, parse JSON, dispatch to identity.upsert_*.
    Returns {op, id, raw} or {op:'none', reason}."""
    raw = await llm_chat(
        messages=[{"role": "user", "content": text}],
        system=_SYSTEM,
        capability="chat:smart",
    )
    raw = raw.strip()
    # Some models wrap JSON in ```json blocks; strip.
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("editor: bad JSON from LLM: %s", exc)
        return {"op": "none", "reason": f"bad LLM JSON: {raw[:120]}"}

    op = spec.get("op")
    try:
        if op == "new_goal":
            nid = await ID.upsert_goal(
                title=spec["title"], metric=spec.get("metric"),
                deadline=spec.get("deadline"),
                about_ids=spec.get("about") or [],
            )
            return {"op": op, "id": nid, "raw": spec}
        if op == "new_value":
            nid = await ID.upsert_value(
                statement=spec["statement"],
                tool_pattern=spec.get("tool_pattern"),
            )
            return {"op": op, "id": nid, "raw": spec}
        if op == "new_nogo":
            nid = await ID.upsert_nogo(
                description=spec["description"],
                tool_pattern=spec["tool_pattern"],
                targets=spec.get("targets") or [],
            )
            return {"op": op, "id": nid, "raw": spec}
        if op == "new_style":
            nid = await ID.upsert_style(
                relationship_id=spec["relationship_id"],
                tone=spec["tone"],
                examples=spec.get("examples") or [],
            )
            return {"op": op, "id": nid, "raw": spec}
        if op == "deactivate":
            ok = await ID.deactivate(spec["label"], spec["id"])
            return {"op": op, "ok": ok, "raw": spec}
        return {"op": "none", "reason": spec.get("reason", "unknown op")}
    except KeyError as exc:
        return {"op": "none", "reason": f"missing field {exc}"}
    except Exception as exc:
        log.exception("editor: apply failed: %s", exc)
        return {"op": "none", "reason": str(exc)[:200]}

"""Single-event and group-batch LLM triage calls, plus embedding batching.

These raise on failure (timeout, malformed JSON, LLMCallFailed) — callers
(concurrency.py) are responsible for catching and mapping to a
pending/error status.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from vera_shared.db.models import EventRow
from vera_shared.llm.circuit import llm_cooldown_remaining_s
from vera_shared.llm.client import chat_async, embed

from brain_triage.postprocess import postprocess_triage
from brain_triage.prompts import TRIAGE_PROMPT_TEMPLATE, build_batch_prompt
from brain_triage.schemas import TRIAGE_BATCH_JSON_SCHEMA, TRIAGE_JSON_SCHEMA

log = logging.getLogger(__name__)

# chat:fast — дешёвый дефолт; chat:smart — бесплатный фолбэк того же класса
# (замер 2026-07-20: sambanova/DeepSeek-V3.2, $0). Раньше триаж стоял на
# chat:fast и полностью замирал на ~5ч/сутки, когда бесплатный дневной лимит
# chat:fast исчерпывался. Теперь при капе он переключается на chat:smart.
TRIAGE_CAPABILITIES = ("chat:fast", "chat:smart")


async def resolve_triage_capability() -> str | None:
    """Первая triage-ёмкость с закрытым circuit'ом, иначе None (обе капнуты)."""
    for cap in TRIAGE_CAPABILITIES:
        if await llm_cooldown_remaining_s(cap) <= 0:
            return cap
    return None


def _strip_markdown_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text


def _parse_json_object(response_text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        # Попытка вытащить JSON из текста (LLM иногда добавляет prefix/suffix)
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    return parsed


async def triage_one(event_row: EventRow) -> dict[str, Any] | None:
    """Триаж одного события. Возвращает metadata."""
    content = (event_row.content_text or "")[:8000]
    prompt = TRIAGE_PROMPT_TEMPLATE.format(
        source=event_row.source,
        account=event_row.account or "—",
        occurred_at=event_row.occurred_at.isoformat() if event_row.occurred_at else "—",
        content=content,
    )

    response_text, meta = await chat_async(
        messages=[{"role": "user", "content": prompt}],
        capability=await resolve_triage_capability() or "chat:fast",
        response_format=TRIAGE_JSON_SCHEMA,
        max_tokens=1500,
        temperature=0.3,
        workflow="triage",
        event_id=event_row.id,
    )

    parsed = _parse_json_object(_strip_markdown_fence(response_text))

    # Если LLM вернул не dict (массив, строку) — ошибка
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")

    parsed = postprocess_triage(parsed, event_row.source)
    parsed["triaged_by_provider"] = meta.get("provider")
    parsed["triaged_by_model"] = meta.get("model")
    parsed["triaged_at"] = datetime.utcnow().isoformat()
    return parsed


async def triage_group_batch(rows: list[EventRow]) -> dict[int, dict[str, Any] | None]:
    """Триаж ОДНОГО batch'а (≤TRIAGE_GROUP_BATCH_SIZE) групповых сообщений
    ОДНИМ LLM-вызовом. Возвращает {event_id: metadata|None} — None для
    событий, которых LLM не вернула (партиальный ответ, обрезка) — вызывающий
    код возвращает их в pending для одиночного ретрая, ничего не теряется.

    Все rows должны быть из одного вызова (общий workflow='triage', общий
    event_id = id первого события — для usage_log/cost-трейсинга; broker не
    поддерживает multi-event_id на один call).
    """
    if not rows:
        return {}
    prompt = build_batch_prompt(rows)

    response_text, meta = await chat_async(
        messages=[{"role": "user", "content": prompt}],
        capability=await resolve_triage_capability() or "chat:fast",
        response_format=TRIAGE_BATCH_JSON_SCHEMA,
        max_tokens=350 * len(rows) + 500,
        temperature=0.3,
        workflow="triage",
        event_id=rows[0].id,
    )

    parsed = _parse_json_object(_strip_markdown_fence(response_text))

    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        raise ValueError(f"Expected {{'results': [...]}}, got {type(parsed).__name__}")

    by_id: dict[int, dict[str, Any]] = {}
    src_by_id = {r.id: r.source for r in rows}
    for item in parsed["results"]:
        if not isinstance(item, dict):
            continue
        eid = item.get("event_id")
        if not isinstance(eid, int) or eid not in src_by_id:
            continue  # LLM выдумала event_id — игнорируем, не наше событие
        item.pop("event_id", None)
        item = postprocess_triage(item, src_by_id[eid])
        item["triaged_by_provider"] = meta.get("provider")
        item["triaged_by_model"] = meta.get("model")
        item["triaged_at"] = datetime.utcnow().isoformat()
        by_id[eid] = item

    # Событие, которое LLM не вернула (обрезка/пропуск) — None, вызывающий
    # код вернёт его в pending на одиночный ретрай, не потеряет.
    return {r.id: by_id.get(r.id) for r in rows}


async def _embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Один Voyage-запрос на N текстов."""
    if not texts:
        return []
    try:
        vectors = await embed(texts)
        if len(vectors) != len(texts):
            log.warning("Embed mismatch: got %d, expected %d", len(vectors), len(texts))
            return [None] * len(texts)
        return vectors
    except Exception as e:
        log.warning("Batch embed failed: %s", e)
        return [None] * len(texts)

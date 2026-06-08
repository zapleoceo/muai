"""Worker loop: SELECT pending events → triage + embed → UPDATE."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.llm.client import LLMCallFailed, chat, embed
from vera_shared.events.schema import TriageMetadata

log = logging.getLogger(__name__)

POLL_INTERVAL_S = float(os.environ.get("TRIAGE_POLL_INTERVAL_S", "10"))
BATCH_SIZE = int(os.environ.get("TRIAGE_BATCH_SIZE", "5"))
PACE_BETWEEN_S = float(os.environ.get("TRIAGE_PACE_S", "2"))


TRIAGE_PROMPT_TEMPLATE = """Ты — Вера, цифровая память Димы. Прочитай событие и извлеки структуру.

Контекст Димы (его текущая жизнь):
- Branch Director IT STEP Academy Jakarta с апреля 2026
- Переезд в Индонезию, виза, KPI команды
- Сосуществелец бара Veranda во Вьетнаме
- Жена Маша, дочь Лиза
- Босс Дмитрий Егоров (yegorov@itstep.org)

Событие (источник={source}, account={account}, occurred_at={occurred_at}):
---
{content}
---

Верни СТРОГО JSON по схеме:
{{
  "importance": <0-100, насколько Дима должен это видеть>,
  "topics": [<список тем>],
  "people_mentioned": [<упомянутые люди>],
  "signals": [
    {{"type": "task|event|news|offer|question|decision|anomaly",
      "summary": "<краткое>",
      "date": "<ISO дата если есть, иначе null>"}}
  ],
  "active_topic_matches": [
    {{"topic": "<тема>", "confidence": 0.0-1.0, "why": "<почему>"}}
  ],
  "needs_action": <true/false>
}}

ВАЖНО: только JSON, без префиксов и комментариев."""


JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "importance": {"type": "integer", "minimum": 0, "maximum": 100},
        "topics": {"type": "array", "items": {"type": "string"}},
        "people_mentioned": {"type": "array", "items": {"type": "string"}},
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "summary": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                },
                "required": ["type", "summary"],
            },
        },
        "active_topic_matches": {"type": "array"},
        "needs_action": {"type": "boolean"},
    },
    "required": ["importance", "topics", "people_mentioned", "signals", "needs_action"],
}


async def triage_one(event_row: EventRow) -> dict[str, Any] | None:
    """Триаж одного события. Возвращает metadata или None при провале."""
    content = (event_row.content_text or "")[:8000]
    prompt = TRIAGE_PROMPT_TEMPLATE.format(
        source=event_row.source,
        account=event_row.account or "—",
        occurred_at=event_row.occurred_at.isoformat() if event_row.occurred_at else "—",
        content=content,
    )

    text, meta = await chat(
        messages=[{"role": "user", "content": prompt}],
        capability="chat:fast",
        require_json_schema=True,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "triage", "strict": True, "schema": JSON_SCHEMA},
        },
        max_tokens=1500,
        temperature=0.3,
        workflow="triage",
        event_id=event_row.id,
    )

    # Парсим JSON — возможно с обёрткой markdown
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    parsed = json.loads(text)
    parsed["triaged_by_provider"] = meta.get("provider")
    parsed["triaged_by_model"] = meta.get("model")
    parsed["triaged_at"] = datetime.utcnow().isoformat()
    return parsed


async def process_pending() -> int:
    """Выбрать pending events, обработать batch."""
    async with get_session() as s:
        rows = (await s.execute(
            select(EventRow)
            .where(EventRow.triage_status == "pending")
            .where(EventRow.content_text != "")
            .order_by(EventRow.occurred_at.desc())
            .limit(BATCH_SIZE)
        )).scalars().all()

    if not rows:
        return 0

    processed = 0
    for row in rows:
        try:
            # 1. Triage via LLM
            metadata = await triage_one(row)
            if metadata is None:
                continue

            # 2. Embed для семантического поиска
            try:
                vectors = await embed(row.content_text[:8000])
                embedding = vectors[0] if vectors else None
            except Exception as e:
                log.warning("Embed failed for event %s: %s", row.id, e)
                embedding = None

            # 3. Update DB
            async with get_session() as s:
                await s.execute(
                    update(EventRow).where(EventRow.id == row.id).values(
                        triage_status="done",
                        triage_metadata=metadata,
                        importance=metadata.get("importance"),
                        embedding_voyage_3=embedding,
                    )
                )
            processed += 1
            log.info("Triaged event %s (importance=%s)",
                     row.id, metadata.get("importance"))

        except LLMCallFailed as e:
            log.warning("LLM exhausted for event %s: %s", row.id, e)
            # Не помечаем error — попробуем ещё раз в следующем тике
            break  # все провайдеры заняты — пауза
        except Exception as e:
            log.exception("Triage failed for event %s: %s", row.id, e)
            async with get_session() as s:
                await s.execute(
                    update(EventRow).where(EventRow.id == row.id).values(
                        triage_status="error",
                        triage_error=str(e)[:500],
                    )
                )
        await asyncio.sleep(PACE_BETWEEN_S)

    return processed


async def main_loop() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("brain-triage worker started, poll=%ss batch=%s", POLL_INTERVAL_S, BATCH_SIZE)

    while True:
        try:
            n = await process_pending()
            if n == 0:
                await asyncio.sleep(POLL_INTERVAL_S)
            else:
                log.info("Processed batch of %s events", n)
        except Exception as e:
            log.exception("Outer loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_S * 2)


if __name__ == "__main__":
    asyncio.run(main_loop())
